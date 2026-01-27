// Package protocol implements the FPV UDP wire format per FPV_PLAN.md Appendix A.
// All multi-byte integers are big-endian (network order).
// All structures are packed with no padding.
package protocol

import (
	"encoding/binary"
	"errors"
)

// Protocol constants
const (
	Version        = 1
	MaxPayloadSize = 1200 // UDP payload target to avoid IP fragmentation
)

// Message types
const (
	MsgTypeVideoFragment = 0x01
	MsgTypeKeepalive     = 0x02
	MsgTypeIDRRequest    = 0x03
	MsgTypeProbe         = 0x04
	MsgTypeHello         = 0x05
)

// Video flags
const (
	FlagKeyframe = 1 << 0 // bit0: IDR frame
	FlagSPSPPS   = 1 << 1 // bit1: contains SPS/PPS
)

// Codec types
const (
	CodecH264 = 1
)

// Roles
const (
	RolePi  = 1
	RoleMac = 2
)

// IDR request reasons
const (
	IDRReasonStartup     = 1
	IDRReasonDecodeError = 2
	IDRReasonLoss        = 3
	IDRReasonUser        = 4
)

// Header sizes
const (
	CommonHeaderSize        = 8
	VideoFragmentHeaderSize = 28
	KeepaliveHeaderSize     = 20
	IDRRequestHeaderSize    = 20
	ProbeHeaderSize         = 28
	HelloHeaderSize         = 32
)

// Errors
var (
	ErrBufferTooSmall = errors.New("buffer too small")
	ErrInvalidVersion = errors.New("invalid protocol version")
	ErrInvalidMsgType = errors.New("invalid message type")
)

// CommonHeader is the 8-byte header present in all messages.
//
//	Offset | Size | Type | Name
//	     0 |    1 | u8   | msg_type
//	     1 |    1 | u8   | version
//	     2 |    2 | u16  | header_len
//	     4 |    4 | u32  | session_id
type CommonHeader struct {
	MsgType   uint8
	Version   uint8
	HeaderLen uint16
	SessionID uint32
}

// Marshal writes the common header to buf (must be >= 8 bytes).
func (h *CommonHeader) Marshal(buf []byte) error {
	if len(buf) < CommonHeaderSize {
		return ErrBufferTooSmall
	}
	buf[0] = h.MsgType
	buf[1] = h.Version
	binary.BigEndian.PutUint16(buf[2:4], h.HeaderLen)
	binary.BigEndian.PutUint32(buf[4:8], h.SessionID)
	return nil
}

// Unmarshal reads the common header from buf.
func (h *CommonHeader) Unmarshal(buf []byte) error {
	if len(buf) < CommonHeaderSize {
		return ErrBufferTooSmall
	}
	h.MsgType = buf[0]
	h.Version = buf[1]
	h.HeaderLen = binary.BigEndian.Uint16(buf[2:4])
	h.SessionID = binary.BigEndian.Uint32(buf[4:8])
	return nil
}

// VideoFragment is msg_type=0x01, sent Pi→macOS.
//
//	Offset | Size | Type  | Name
//	     8 |    4 | u32   | stream_id
//	    12 |    4 | u32   | frame_id
//	    16 |    2 | u16   | frag_index
//	    18 |    2 | u16   | frag_count
//	    20 |    4 | u32   | ts_ms
//	    24 |    1 | u8    | flags
//	    25 |    1 | u8    | codec
//	    26 |    2 | u16   | payload_len
//	    28 |    N | bytes | payload
type VideoFragment struct {
	SessionID  uint32
	StreamID   uint32
	FrameID    uint32
	FragIndex  uint16
	FragCount  uint16
	TsMs       uint32
	Flags      uint8
	Codec      uint8
	PayloadLen uint16
	Payload    []byte
}

// Marshal writes the video fragment to buf. Returns bytes written.
func (v *VideoFragment) Marshal(buf []byte) (int, error) {
	total := VideoFragmentHeaderSize + len(v.Payload)
	if len(buf) < total {
		return 0, ErrBufferTooSmall
	}

	// Common header
	buf[0] = MsgTypeVideoFragment
	buf[1] = Version
	binary.BigEndian.PutUint16(buf[2:4], VideoFragmentHeaderSize)
	binary.BigEndian.PutUint32(buf[4:8], v.SessionID)

	// Type-specific
	binary.BigEndian.PutUint32(buf[8:12], v.StreamID)
	binary.BigEndian.PutUint32(buf[12:16], v.FrameID)
	binary.BigEndian.PutUint16(buf[16:18], v.FragIndex)
	binary.BigEndian.PutUint16(buf[18:20], v.FragCount)
	binary.BigEndian.PutUint32(buf[20:24], v.TsMs)
	buf[24] = v.Flags
	buf[25] = v.Codec
	binary.BigEndian.PutUint16(buf[26:28], uint16(len(v.Payload)))

	// Payload
	copy(buf[28:], v.Payload)
	return total, nil
}

// Unmarshal reads a video fragment from buf.
func (v *VideoFragment) Unmarshal(buf []byte) error {
	if len(buf) < VideoFragmentHeaderSize {
		return ErrBufferTooSmall
	}
	if buf[0] != MsgTypeVideoFragment {
		return ErrInvalidMsgType
	}
	if buf[1] != Version {
		return ErrInvalidVersion
	}

	v.SessionID = binary.BigEndian.Uint32(buf[4:8])
	v.StreamID = binary.BigEndian.Uint32(buf[8:12])
	v.FrameID = binary.BigEndian.Uint32(buf[12:16])
	v.FragIndex = binary.BigEndian.Uint16(buf[16:18])
	v.FragCount = binary.BigEndian.Uint16(buf[18:20])
	v.TsMs = binary.BigEndian.Uint32(buf[20:24])
	v.Flags = buf[24]
	v.Codec = buf[25]
	v.PayloadLen = binary.BigEndian.Uint16(buf[26:28])

	if len(buf) < VideoFragmentHeaderSize+int(v.PayloadLen) {
		return ErrBufferTooSmall
	}
	v.Payload = buf[28 : 28+v.PayloadLen]
	return nil
}

// IsKeyframe returns true if this fragment belongs to a keyframe.
func (v *VideoFragment) IsKeyframe() bool {
	return v.Flags&FlagKeyframe != 0
}

// HasSPSPPS returns true if this AU contains SPS/PPS.
func (v *VideoFragment) HasSPSPPS() bool {
	return v.Flags&FlagSPSPPS != 0
}

// Keepalive is msg_type=0x02, sent both directions.
//
//	Offset | Size | Type | Name
//	     8 |    4 | u32  | ts_ms
//	    12 |    4 | u32  | seq
//	    16 |    4 | u32  | echo_ts_ms
type Keepalive struct {
	SessionID uint32
	TsMs      uint32
	Seq       uint32
	EchoTsMs  uint32
}

// Marshal writes the keepalive to buf. Returns bytes written.
func (k *Keepalive) Marshal(buf []byte) (int, error) {
	if len(buf) < KeepaliveHeaderSize {
		return 0, ErrBufferTooSmall
	}

	buf[0] = MsgTypeKeepalive
	buf[1] = Version
	binary.BigEndian.PutUint16(buf[2:4], KeepaliveHeaderSize)
	binary.BigEndian.PutUint32(buf[4:8], k.SessionID)
	binary.BigEndian.PutUint32(buf[8:12], k.TsMs)
	binary.BigEndian.PutUint32(buf[12:16], k.Seq)
	binary.BigEndian.PutUint32(buf[16:20], k.EchoTsMs)
	return KeepaliveHeaderSize, nil
}

// Unmarshal reads a keepalive from buf.
func (k *Keepalive) Unmarshal(buf []byte) error {
	if len(buf) < KeepaliveHeaderSize {
		return ErrBufferTooSmall
	}
	if buf[0] != MsgTypeKeepalive {
		return ErrInvalidMsgType
	}
	if buf[1] != Version {
		return ErrInvalidVersion
	}

	k.SessionID = binary.BigEndian.Uint32(buf[4:8])
	k.TsMs = binary.BigEndian.Uint32(buf[8:12])
	k.Seq = binary.BigEndian.Uint32(buf[12:16])
	k.EchoTsMs = binary.BigEndian.Uint32(buf[16:20])
	return nil
}

// IDRRequest is msg_type=0x03, sent macOS→Pi.
//
//	Offset | Size | Type  | Name
//	     8 |    4 | u32   | seq
//	    12 |    4 | u32   | ts_ms
//	    16 |    1 | u8    | reason
//	    17 |    3 | bytes | reserved
type IDRRequest struct {
	SessionID uint32
	Seq       uint32
	TsMs      uint32
	Reason    uint8
}

// Marshal writes the IDR request to buf. Returns bytes written.
func (r *IDRRequest) Marshal(buf []byte) (int, error) {
	if len(buf) < IDRRequestHeaderSize {
		return 0, ErrBufferTooSmall
	}

	buf[0] = MsgTypeIDRRequest
	buf[1] = Version
	binary.BigEndian.PutUint16(buf[2:4], IDRRequestHeaderSize)
	binary.BigEndian.PutUint32(buf[4:8], r.SessionID)
	binary.BigEndian.PutUint32(buf[8:12], r.Seq)
	binary.BigEndian.PutUint32(buf[12:16], r.TsMs)
	buf[16] = r.Reason
	buf[17] = 0 // reserved
	buf[18] = 0
	buf[19] = 0
	return IDRRequestHeaderSize, nil
}

// Unmarshal reads an IDR request from buf.
func (r *IDRRequest) Unmarshal(buf []byte) error {
	if len(buf) < IDRRequestHeaderSize {
		return ErrBufferTooSmall
	}
	if buf[0] != MsgTypeIDRRequest {
		return ErrInvalidMsgType
	}
	if buf[1] != Version {
		return ErrInvalidVersion
	}

	r.SessionID = binary.BigEndian.Uint32(buf[4:8])
	r.Seq = binary.BigEndian.Uint32(buf[8:12])
	r.TsMs = binary.BigEndian.Uint32(buf[12:16])
	r.Reason = buf[16]
	return nil
}

// Probe is msg_type=0x04, used for UDP hole punching.
//
//	Offset | Size | Type | Name
//	     8 |    4 | u32  | ts_ms
//	    12 |    4 | u32  | probe_seq
//	    16 |    8 | u64  | nonce
//	    24 |    1 | u8   | role
//	    25 |    1 | u8   | flags
//	    26 |    2 | u16  | reserved
type Probe struct {
	SessionID uint32
	TsMs      uint32
	ProbeSeq  uint32
	Nonce     uint64
	Role      uint8
	Flags     uint8
}

// Marshal writes the probe to buf. Returns bytes written.
func (p *Probe) Marshal(buf []byte) (int, error) {
	if len(buf) < ProbeHeaderSize {
		return 0, ErrBufferTooSmall
	}

	buf[0] = MsgTypeProbe
	buf[1] = Version
	binary.BigEndian.PutUint16(buf[2:4], ProbeHeaderSize)
	binary.BigEndian.PutUint32(buf[4:8], p.SessionID)
	binary.BigEndian.PutUint32(buf[8:12], p.TsMs)
	binary.BigEndian.PutUint32(buf[12:16], p.ProbeSeq)
	binary.BigEndian.PutUint64(buf[16:24], p.Nonce)
	buf[24] = p.Role
	buf[25] = p.Flags
	buf[26] = 0 // reserved
	buf[27] = 0
	return ProbeHeaderSize, nil
}

// Unmarshal reads a probe from buf.
func (p *Probe) Unmarshal(buf []byte) error {
	if len(buf) < ProbeHeaderSize {
		return ErrBufferTooSmall
	}
	if buf[0] != MsgTypeProbe {
		return ErrInvalidMsgType
	}
	if buf[1] != Version {
		return ErrInvalidVersion
	}

	p.SessionID = binary.BigEndian.Uint32(buf[4:8])
	p.TsMs = binary.BigEndian.Uint32(buf[8:12])
	p.ProbeSeq = binary.BigEndian.Uint32(buf[12:16])
	p.Nonce = binary.BigEndian.Uint64(buf[16:24])
	p.Role = buf[24]
	p.Flags = buf[25]
	return nil
}

// Hello is msg_type=0x05, optional capabilities exchange.
//
//	Offset | Size | Type  | Name
//	     8 |    2 | u16   | width
//	    10 |    2 | u16   | height
//	    12 |    2 | u16   | fps_x10
//	    14 |    4 | u32   | bitrate_bps
//	    18 |    1 | u8    | avc_profile
//	    19 |    1 | u8    | avc_level
//	    20 |    4 | u32   | idr_interval_frames
//	    24 |    8 | bytes | reserved
type Hello struct {
	SessionID         uint32
	Width             uint16
	Height            uint16
	FpsX10            uint16
	BitrateBps        uint32
	AVCProfile        uint8
	AVCLevel          uint8
	IDRIntervalFrames uint32
}

// Marshal writes the hello to buf. Returns bytes written.
func (h *Hello) Marshal(buf []byte) (int, error) {
	if len(buf) < HelloHeaderSize {
		return 0, ErrBufferTooSmall
	}

	buf[0] = MsgTypeHello
	buf[1] = Version
	binary.BigEndian.PutUint16(buf[2:4], HelloHeaderSize)
	binary.BigEndian.PutUint32(buf[4:8], h.SessionID)
	binary.BigEndian.PutUint16(buf[8:10], h.Width)
	binary.BigEndian.PutUint16(buf[10:12], h.Height)
	binary.BigEndian.PutUint16(buf[12:14], h.FpsX10)
	binary.BigEndian.PutUint32(buf[14:18], h.BitrateBps)
	buf[18] = h.AVCProfile
	buf[19] = h.AVCLevel
	binary.BigEndian.PutUint32(buf[20:24], h.IDRIntervalFrames)
	// reserved bytes 24-31
	for i := 24; i < 32; i++ {
		buf[i] = 0
	}
	return HelloHeaderSize, nil
}

// Unmarshal reads a hello from buf.
func (h *Hello) Unmarshal(buf []byte) error {
	if len(buf) < HelloHeaderSize {
		return ErrBufferTooSmall
	}
	if buf[0] != MsgTypeHello {
		return ErrInvalidMsgType
	}
	if buf[1] != Version {
		return ErrInvalidVersion
	}

	h.SessionID = binary.BigEndian.Uint32(buf[4:8])
	h.Width = binary.BigEndian.Uint16(buf[8:10])
	h.Height = binary.BigEndian.Uint16(buf[10:12])
	h.FpsX10 = binary.BigEndian.Uint16(buf[12:14])
	h.BitrateBps = binary.BigEndian.Uint32(buf[14:18])
	h.AVCProfile = buf[18]
	h.AVCLevel = buf[19]
	h.IDRIntervalFrames = binary.BigEndian.Uint32(buf[20:24])
	return nil
}

// ParseMsgType returns the message type from the first byte of a packet.
func ParseMsgType(buf []byte) (uint8, error) {
	if len(buf) < 1 {
		return 0, ErrBufferTooSmall
	}
	return buf[0], nil
}

// IsNewer compares two frame IDs with wrap-around handling (RFC 1982 serial arithmetic).
// Returns true if a is newer than b.
func IsNewer(a, b uint32) bool {
	return int32(a-b) > 0
}

// IsOlder compares two frame IDs with wrap-around handling.
// Returns true if a is older than b.
func IsOlder(a, b uint32) bool {
	return int32(a-b) < 0
}
