import numpy as np
from gnuradio import gr
import datetime

try:
    import pmt
except ImportError:
    try:
        from gnuradio import pmt
    except ImportError:
        pass
# -----------------------------

class rds_encoder_simplified(gr.sync_block):
    """
    Simplified RDS (Radio Data System) Encoder.
    Implements standard framing and baseband bitstream generation.
    Supports dynamic asynchronous updates for PS (Program Service) and RT (Radio Text) via PMT.
    """
    def __init__(self, ps_name="MOJERADIO", radio_text="Test RDS transmission from GNU Radio"):
        gr.sync_block.__init__(self,
            name='RDS Encoder Simple',
            in_sig=None,
            out_sig=[np.uint8]) # Output: Raw baseband bitstream (0s and 1s)

        # Register message port for runtime configuration (dynamic text updates)
        if 'pmt' in globals():
            self.message_port_register_in(pmt.intern("rds in"))
            self.set_msg_handler(pmt.intern("rds in"), self.handle_msg)

        # --- RDS PROTOCOL CONSTANTS & DEFAULTS ---
        self.PI = 0x3012         # Program Identification code (Hex)
        self.PTY = 10            # Program Type (10 = Pop Music)
        self.TP = True           # Traffic Program flag
        self.TA = False          # Traffic Announcement flag
        self.MS = True           # Music/Speech switch
        self.AF1 = 89.0          # Alternative Frequency
        
        # Checkword Offset Words for Block Synchronization (Standard IEC 62106)
        # Order: A, B, C, C', D
        self.OFFSET_WORDS = [0x0FC, 0x198, 0x168, 0x350, 0x1B4]

        # Active groups scheduling configuration
        self.active_groups = {
            0: 1,  # Group 0A: Basic Tuning and Switching (PS)
            1: 1,  # Group 1A: Program Item Number and PIN/ECC
            2: 1,  # Group 2A: Radio Text (RT)
            4: 1,  # Group 4A: Clock Time and Date (CT)
            11: 1  # Group 11A: Open Data Application
        }
        
        # Internal state initialization
        self.ps_text = ""
        self.rt_text = ""
        self.set_ps_internal(ps_name)
        self.set_radiotext_internal(radio_text)

        self.d_g0_counter = 0
        self.d_g2_counter = 0
        self.buffers = [] 
        self.d_current_buffer_idx = 0
        self.d_buffer_bit_counter = 0
        
        # Pre-build the initial bitstream sequence
        self.rebuild()

    # --- STRING FORMATTING UTILS ---
    def set_ps_internal(self, text):
        """Formats the PS name to exactly 8 characters as per standard."""
        clean = str(text).upper().replace('\n', '')[:8]
        self.ps_text = clean.ljust(8)

    def set_radiotext_internal(self, text):
        """Formats the Radio Text to exactly 64 characters."""
        clean = str(text).replace('\n', '')[:64]
        self.rt_text = clean.ljust(64)

    # --- ASYNCHRONOUS MESSAGE HANDLER ---
    def handle_msg(self, msg):
        """
        Handles incoming PMT messages to modify RDS data without stopping the flowgraph.
        Accepts formats: 'ps <text>' or 'text <text>'.
        """
        if not pmt.is_pair(msg): return
        try:
            inp = str(pmt.blob_data(pmt.cdr(msg)).tobytes().decode('ascii')).strip()
            parts = inp.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            
            if cmd == "ps":
                self.set_ps_internal(arg)
                self.rebuild()
            elif cmd in ["text", "rt"]:
                self.set_radiotext_internal(arg)
                self.rebuild()
        except:
            pass # Failsafe against malformed PMT payloads

    # --- L2 FRAMING & ERROR CORRECTION ---
    def calc_syndrome(self, message, mlen):
        """
        Calculates the 10-bit checkword (CRC/Syndrome) using standard RDS generator polynomial.
        Generator Polynomial: g(x) = x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1 (0x5B9)
        """
        reg = 0
        poly = 0x5B9 
        plen = 10
        # Polynomial division algorithm
        for i in range(mlen, 0, -1):
            reg = (reg << 1) | ((message >> (i - 1)) & 0x01)
            if (reg & (1 << plen)):
                reg = reg ^ poly
        for i in range(plen, 0, -1):
            reg = reg << 1
            if (reg & (1 << plen)):
                reg = reg ^ poly
        return reg & ((1 << plen) - 1)

    def encode_af(self, af):
        """Encodes Alternative Frequency (Method A)."""
        if 87.6 <= af <= 107.9: return int(round((af - 87.5) * 10))
        return 0

    # --- BITSTREAM RECONSTRUCTION ---
    def rebuild(self):
        """
        Constructs the complete sequence of physical bits ready for modulation.
        Schedules groups according to priority (e.g., PS needs higher repetition rate than RT).
        """
        self.buffers = []
        
        # 32 time-slot scheduler loop
        for i in range(32):
            g_type = i % 16
            is_B = (i >= 16) 
            
            if self.active_groups.get(g_type, 0) == 1:
                repeats = 1
                if g_type == 0: repeats = 4    # PS needs frequent broadcasting
                elif g_type == 2: repeats = 16 # RT requires 16 segments to transmit 64 chars
                
                for _ in range(repeats):
                    self.create_group(g_type, is_B)
        
        self.d_current_buffer_idx = 0

    def create_group(self, group_type, AB_flag):
        """Assembles a 104-bit RDS group consisting of 4 blocks (26 bits each)."""
        infoword = [0, 0, 0, 0] # 4 words, 16 bits each
        
        # Block 1: PI Code
        infoword[0] = self.PI
        
        # Block 2: Header (Group Type, Traffic Codes, PTY)
        # Structure: [GroupType(4)][AB(1)][TP(1)][PTY(5)][Remaining(5)]
        infoword[1] = ((group_type & 0xF) << 12) | \
                      ((1 if AB_flag else 0) << 11) | \
                      ((1 if self.TP else 0) << 10) | \
                      ((self.PTY & 0x1F) << 5)

        # Group-specific data payload formatting
        if group_type == 0: self.prepare_group0(infoword, AB_flag)
        elif group_type == 1: self.prepare_group1(infoword)
        elif group_type == 2: self.prepare_group2(infoword, AB_flag)
        elif group_type == 4: self.prepare_group4(infoword)
        elif group_type == 11: self.prepare_group11(infoword)

        # Calculate CRC and apply Offset Words (A, B, C/C', D)
        final_block_bits = np.zeros(104, dtype=np.uint8)
        
        for i in range(4):
            # 1. CRC for 16 payload bits
            crc = self.calc_syndrome(infoword[i], 16)
            # 2. Assemble 26-bit block (16 payload + 10 checkword)
            raw = ((infoword[i] & 0xFFFF) << 10) | (crc & 0x3FF)
            # 3. Select appropriate offset word for synchronization
            off_idx = i
            if i == 2 and AB_flag: off_idx = 3 # Offset C' for Version B
            elif i == 3: off_idx = 4           # Offset D
            
            # 4. XOR masking
            val_with_offset = raw ^ self.OFFSET_WORDS[off_idx]

            # 5. Serialize to bits (Big Endian)
            for b in range(26):
                bit = (val_with_offset >> (25 - b)) & 0x1
                final_block_bits[i*26 + b] = bit
        
        self.buffers.append(final_block_bits)

    # --- GROUP PAYLOAD HANDLERS ---
    def prepare_group0(self, info, AB):
        """Group 0A: Basic tuning, TA/MS flags, and PS Name segments."""
        info[1] |= ((1 if self.TA else 0) << 4) | ((1 if self.MS else 0) << 3)
        info[1] |= (self.d_g0_counter & 0x3)
        if self.d_g0_counter == 3: info[1] |= 0x4
        
        # Block 3: AF
        info[2] = (225 << 8) | (self.encode_af(self.AF1) & 0xFF)
        
        # Block 4: 2 characters of PS Name
        idx = self.d_g0_counter * 2
        c1 = ord(self.ps_text[idx])
        c2 = ord(self.ps_text[idx+1])
        info[3] = (c1 << 8) | c2
        
        self.d_g0_counter = (self.d_g0_counter + 1) % 4

    def prepare_group2(self, info, AB):
        """Group 2A: Radio Text (RT) segments."""
        info[1] |= (self.d_g2_counter & 0xF)
        
        # Blocks 3 & 4: 4 characters of Radio Text
        idx = self.d_g2_counter * 4
        c1, c2 = ord(self.rt_text[idx]), ord(self.rt_text[idx+1])
        c3, c4 = ord(self.rt_text[idx+2]), ord(self.rt_text[idx+3])
        
        info[2] = (c1 << 8) | c2
        info[3] = (c3 << 8) | c4
        
        self.d_g2_counter = (self.d_g2_counter + 1) % 16

    def prepare_group4(self, info):
        """Group 4A: Clock Time (CT) calculated via Modified Julian Date."""
        now = datetime.datetime.utcnow()
        Y, M, D = now.year, now.month, now.day
        h, m = now.hour, now.minute
        
        L = 1 if (M <= 2) else 0
        mjd = 14956 + D + int((Y - L) * 365.25) + int((M + 1 + L * 12) * 30.6001)
        
        info[1] |= ((mjd >> 15) & 0x3)
        info[2] = (((mjd >> 7) & 0xFF) << 8) | ((mjd & 0x7F) << 1) | ((h >> 4) & 0x1)
        info[3] = ((h & 0xF) << 12) | (((m >> 2) & 0xF) << 8) | ((m & 0x3) << 6)

    def prepare_group1(self, info):
        info[2], info[3] = 0x80E0, 0x0000
        
    def prepare_group11(self, info):
        info[1] |= 0x1C8
        info[2], info[3] = 0x2038, 0x4456

    # --- DSP WORK STREAM ---
    def work(self, input_items, output_items):
        """
        High-throughput GNU Radio work function.
        Avoids slow Python 'for' loops in favor of vectorized NumPy array slicing.
        """
        out = output_items[0]
        n_out = len(out)
        if not self.buffers: return 0

        written = 0
        while written < n_out:
            current_buf = self.buffers[self.d_current_buffer_idx]
            available = len(current_buf) - self.d_buffer_bit_counter
            to_write = min(available, n_out - written)
            
            # Vectorized memory block copy
            out[written : written + to_write] = current_buf[self.d_buffer_bit_counter : self.d_buffer_bit_counter + to_write]
            
            written += to_write
            self.d_buffer_bit_counter += to_write
            
            if self.d_buffer_bit_counter >= len(current_buf):
                self.d_buffer_bit_counter = 0
                self.d_current_buffer_idx = (self.d_current_buffer_idx + 1) % len(self.buffers)

        return n_out