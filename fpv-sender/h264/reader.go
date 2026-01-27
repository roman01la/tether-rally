// Package h264 provides utilities for reading and parsing H.264 Annex B streams.
package h264

import (
	"errors"
	"io"
)

// NAL unit types
const (
	NALTypeSlice  = 1  // Non-IDR slice
	NALTypeIDR    = 5  // IDR slice (keyframe)
	NALTypeSEI    = 6  // Supplemental enhancement info
	NALTypeSPS    = 7  // Sequence parameter set
	NALTypePPS    = 8  // Picture parameter set
	NALTypeAUD    = 9  // Access unit delimiter
	NALTypeFiller = 12 // Filler data
)

// isFirstSliceInFrame checks if a slice NAL is the first slice of a new frame.
// It parses the first_mb_in_slice field (ue(v) coded) from the slice header.
// If first_mb_in_slice == 0, this is the start of a new frame.
func isFirstSliceInFrame(nalData []byte) bool {
	// NAL data includes start code + header byte + payload
	// Find the payload start (skip start code and header byte)
	payloadStart := 0
	if len(nalData) >= 4 && nalData[0] == 0 && nalData[1] == 0 && nalData[2] == 0 && nalData[3] == 1 {
		payloadStart = 5 // 4-byte start code + 1 header byte
	} else if len(nalData) >= 3 && nalData[0] == 0 && nalData[1] == 0 && nalData[2] == 1 {
		payloadStart = 4 // 3-byte start code + 1 header byte
	} else {
		return true // Can't parse, assume new frame
	}

	if payloadStart >= len(nalData) {
		return true
	}

	// Parse first_mb_in_slice using exp-golomb ue(v) decoding
	// first_mb_in_slice is the very first syntax element in slice header
	payload := nalData[payloadStart:]

	// Exp-golomb: find leading zeros, then read that many bits + 1
	// If first_mb_in_slice == 0, the codeword is just "1" (no leading zeros)
	if len(payload) == 0 {
		return true
	}

	// Check if first bit is 1 - that means first_mb_in_slice == 0
	if (payload[0] & 0x80) != 0 {
		return true // first_mb_in_slice == 0, new frame
	}

	// Otherwise there are leading zeros, meaning first_mb_in_slice > 0
	// This is a continuation slice, not the start of a new frame
	return false
}

// H.264 profiles
const (
	ProfileBaseline = 66
	ProfileMain     = 77
	ProfileHigh     = 100
)

// Errors
var (
	ErrNoStartCode = errors.New("no start code found")
	ErrShortNAL    = errors.New("NAL unit too short")
)

// NALUnit represents a single NAL unit.
type NALUnit struct {
	Type   uint8  // NAL unit type (5 bits)
	RefIDC uint8  // nal_ref_idc (2 bits)
	Data   []byte // Full NAL including header byte
}

// IsKeyframe returns true if this NAL is part of an IDR frame.
func (n *NALUnit) IsKeyframe() bool {
	return n.Type == NALTypeIDR
}

// IsSPS returns true if this is a sequence parameter set.
func (n *NALUnit) IsSPS() bool {
	return n.Type == NALTypeSPS
}

// IsPPS returns true if this is a picture parameter set.
func (n *NALUnit) IsPPS() bool {
	return n.Type == NALTypePPS
}

// AccessUnit represents a complete video frame (one or more NAL units).
type AccessUnit struct {
	NALs       []NALUnit
	IsKeyframe bool   // True if contains IDR
	HasSPSPPS  bool   // True if contains SPS and PPS
	Data       []byte // Complete AU data (Annex B format with start codes)
}

// Reader reads H.264 Annex B NAL units from a stream.
type Reader struct {
	r       io.Reader
	buf     []byte
	pos     int
	end     int
	auBuf   []byte   // Buffer for accumulating AU
	pending *NALUnit // NAL that was read but belongs to next AU
}

// NewReader creates a new H.264 Annex B reader.
func NewReader(r io.Reader) *Reader {
	return &Reader{
		r:     r,
		buf:   make([]byte, 256*1024),    // 256KB read buffer
		auBuf: make([]byte, 0, 128*1024), // 128KB AU buffer
	}
}

// ReadAccessUnit reads the next complete Access Unit.
// An AU is delimited by:
// - AUD NAL (Access Unit Delimiter), or
// - Start of next frame (IDR/slice after non-slice NALs)
func (r *Reader) ReadAccessUnit() (*AccessUnit, error) {
	au := &AccessUnit{
		NALs: make([]NALUnit, 0, 8),
	}
	r.auBuf = r.auBuf[:0]

	sawSlice := false

	// If we have a pending NAL from previous read, use it first
	if r.pending != nil {
		nal := *r.pending
		r.pending = nil

		r.auBuf = append(r.auBuf, nal.Data...)
		au.NALs = append(au.NALs, nal)

		if nal.IsKeyframe() {
			au.IsKeyframe = true
		}
		if nal.IsSPS() || nal.IsPPS() {
			au.HasSPSPPS = true
		}
		if nal.Type == NALTypeSlice || nal.Type == NALTypeIDR {
			sawSlice = true
		}
	}

	for {
		nal, err := r.readNAL()
		if err != nil {
			if err == io.EOF && len(au.NALs) > 0 {
				au.Data = make([]byte, len(r.auBuf))
				copy(au.Data, r.auBuf)
				return au, nil
			}
			return nil, err
		}

		// Check if this NAL starts a new AU
		nalType := nal.Type
		isSlice := nalType == NALTypeSlice || nalType == NALTypeIDR

		// AU boundary detection:
		// 1. AUD (Access Unit Delimiter) always starts a new AU
		// 2. A slice NAL with first_mb_in_slice == 0 starts a new AU
		//    (but only if we've already seen a slice in current AU)
		if nalType == NALTypeAUD {
			if len(au.NALs) > 0 {
				// Save this AUD for next AU
				r.pending = &nal
				au.Data = make([]byte, len(r.auBuf))
				copy(au.Data, r.auBuf)
				return au, nil
			}
			// First NAL is AUD - include it in this AU
		} else if sawSlice && isSlice && isFirstSliceInFrame(nal.Data) {
			// New frame starting - this slice belongs to next AU
			r.pending = &nal
			au.Data = make([]byte, len(r.auBuf))
			copy(au.Data, r.auBuf)
			return au, nil
		}

		// Accumulate NAL with start code
		r.auBuf = append(r.auBuf, nal.Data...)

		au.NALs = append(au.NALs, nal)

		if nal.IsKeyframe() {
			au.IsKeyframe = true
		}
		if nal.IsSPS() || nal.IsPPS() {
			au.HasSPSPPS = true
		}
		if isSlice {
			sawSlice = true
		}
	}
}

// readNAL reads the next NAL unit.
func (r *Reader) readNAL() (NALUnit, error) {
	// Ensure we have data
	if r.pos >= r.end {
		if err := r.fill(); err != nil {
			return NALUnit{}, err
		}
	}

	// Find start code of current NAL
	scPos, scLen, err := r.scanStartCode(r.pos)
	if err != nil {
		return NALUnit{}, err
	}

	// Move position to the start code and mark it as the NAL start
	// We'll copy NAL data as we find it to avoid buffer shift issues
	r.pos = scPos

	// Read NAL data incrementally to handle buffer shifts correctly
	// Start by copying the start code
	nalData := make([]byte, 0, 64*1024) // Start with 64KB capacity
	nalData = append(nalData, r.buf[r.pos:r.pos+scLen]...)
	r.pos += scLen

	// Read the NAL header byte
	if r.pos >= r.end {
		if err := r.fill(); err != nil {
			return NALUnit{}, err
		}
	}
	headerByte := r.buf[r.pos]

	// Now scan for the next start code, copying data as we go
	// This handles buffer compaction correctly because we're copying
	// data before it can be discarded
	for {
		// Look for start code pattern in current buffer
		foundEnd := false
		endPos := r.pos

		for endPos+3 <= r.end {
			if r.buf[endPos] == 0x00 && r.buf[endPos+1] == 0x00 {
				if r.buf[endPos+2] == 0x01 {
					foundEnd = true
					break
				}
				if endPos+3 < r.end && r.buf[endPos+2] == 0x00 && r.buf[endPos+3] == 0x01 {
					foundEnd = true
					break
				}
			}
			endPos++
		}

		if foundEnd {
			// Copy up to the start code
			nalData = append(nalData, r.buf[r.pos:endPos]...)
			r.pos = endPos
			break
		}

		// No start code found - copy what we have and read more
		// Keep 3 bytes in case start code spans read boundary
		keepBytes := 3
		if r.end-r.pos > keepBytes {
			nalData = append(nalData, r.buf[r.pos:r.end-keepBytes]...)
			r.pos = r.end - keepBytes
		}

		// Compact and read more data
		if r.pos > 0 {
			copy(r.buf, r.buf[r.pos:r.end])
			r.end -= r.pos
			r.pos = 0
		}

		n, err := r.r.Read(r.buf[r.end:])
		if n > 0 {
			r.end += n
		}
		if err != nil {
			if err == io.EOF {
				// End of stream - copy remaining data
				nalData = append(nalData, r.buf[r.pos:r.end]...)
				r.pos = r.end
				break
			}
			return NALUnit{}, err
		}
	}

	// Empty NAL - skip it
	if len(nalData) <= 4 {
		return r.readNAL()
	}

	nal := NALUnit{
		Type:   headerByte & 0x1F,
		RefIDC: (headerByte >> 5) & 0x03,
		Data:   nalData,
	}

	return nal, nil
}

// scanStartCode finds the next start code starting from pos.
// Does NOT modify r.pos. May call fill() which shifts the buffer.
// Returns position relative to current buffer state.
func (r *Reader) scanStartCode(from int) (int, int, error) {
	pos := from

	for {
		// Need at least 4 bytes to check for start codes
		for pos+4 > r.end {
			// Need more data - but first compact buffer
			if pos > 0 {
				// Shift buffer to make room
				copy(r.buf, r.buf[pos:r.end])
				r.end -= pos
				r.pos -= pos
				pos = 0
			}

			// Read more
			n, err := r.r.Read(r.buf[r.end:])
			if n > 0 {
				r.end += n
			}
			if err != nil {
				if err == io.EOF && pos < r.end {
					// Some data left, return EOF position
					return r.end, 0, io.EOF
				}
				return 0, 0, err
			}
		}

		// Scan for start code
		for pos+3 <= r.end {
			if r.buf[pos] == 0x00 && r.buf[pos+1] == 0x00 {
				if r.buf[pos+2] == 0x01 {
					return pos, 3, nil
				}
				if pos+3 < r.end && r.buf[pos+2] == 0x00 && r.buf[pos+3] == 0x01 {
					return pos, 4, nil
				}
			}
			pos++
		}
	}
}

// fill reads more data into the buffer, compacting first if needed.
func (r *Reader) fill() error {
	// Compact: move unread data to beginning
	if r.pos > 0 {
		copy(r.buf, r.buf[r.pos:r.end])
		r.end -= r.pos
		r.pos = 0
	}

	// Read more data
	n, err := r.r.Read(r.buf[r.end:])
	r.end += n
	if err != nil {
		return err
	}
	if n == 0 {
		return io.EOF
	}
	return nil
}
