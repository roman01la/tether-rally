// Video Decoder Web Worker
// Handles H.264 decoding via WebCodecs and renders to OffscreenCanvas using WebGL

let canvas = null;
let gl = null;
let glProgram = null;
let glTexture = null;
let videoDecoder = null;
let codecConfig = null;
let h264Sps = null;
let h264Pps = null;
let frameCount = 0;
let lastFrameTime = 0;
let videoWidth = 0;
let videoHeight = 0;

// ===== WebGL Setup =====
const VERTEX_SHADER = `
	attribute vec2 a_position;
	attribute vec2 a_texCoord;
	varying vec2 v_texCoord;
	void main() {
		gl_Position = vec4(a_position, 0.0, 1.0);
		v_texCoord = a_texCoord;
	}
`;

const FRAGMENT_SHADER = `
	precision mediump float;
	uniform sampler2D u_texture;
	varying vec2 v_texCoord;
	void main() {
		gl_FragColor = texture2D(u_texture, v_texCoord);
	}
`;

function initWebGL(canvas) {
	gl = canvas.getContext('webgl', {
		alpha: false,
		antialias: false,
		depth: false,
		desynchronized: true,
		preserveDrawingBuffer: false,
		powerPreference: 'high-performance',
	});

	if (!gl) {
		console.error('[Worker] WebGL not available');
		return false;
	}

	// Compile shaders
	const vertShader = gl.createShader(gl.VERTEX_SHADER);
	gl.shaderSource(vertShader, VERTEX_SHADER);
	gl.compileShader(vertShader);

	const fragShader = gl.createShader(gl.FRAGMENT_SHADER);
	gl.shaderSource(fragShader, FRAGMENT_SHADER);
	gl.compileShader(fragShader);

	// Create program
	glProgram = gl.createProgram();
	gl.attachShader(glProgram, vertShader);
	gl.attachShader(glProgram, fragShader);
	gl.linkProgram(glProgram);
	gl.useProgram(glProgram);

	// Set up geometry (full-screen quad)
	const positions = new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]);
	const texCoords = new Float32Array([0, 1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 0]);

	const posBuffer = gl.createBuffer();
	gl.bindBuffer(gl.ARRAY_BUFFER, posBuffer);
	gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
	const posLoc = gl.getAttribLocation(glProgram, 'a_position');
	gl.enableVertexAttribArray(posLoc);
	gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

	const texBuffer = gl.createBuffer();
	gl.bindBuffer(gl.ARRAY_BUFFER, texBuffer);
	gl.bufferData(gl.ARRAY_BUFFER, texCoords, gl.STATIC_DRAW);
	const texLoc = gl.getAttribLocation(glProgram, 'a_texCoord');
	gl.enableVertexAttribArray(texLoc);
	gl.vertexAttribPointer(texLoc, 2, gl.FLOAT, false, 0, 0);

	// Create texture
	glTexture = gl.createTexture();
	gl.bindTexture(gl.TEXTURE_2D, glTexture);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

	console.log('[Worker] WebGL initialized');
	return true;
}

function renderFrame(frame) {
	// Resize canvas if needed
	if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
		canvas.width = frame.displayWidth;
		canvas.height = frame.displayHeight;
		gl.viewport(0, 0, canvas.width, canvas.height);
	}

	// Track resolution
	videoWidth = frame.displayWidth;
	videoHeight = frame.displayHeight;

	// Upload VideoFrame directly as texture (zero-copy on supported platforms)
	gl.bindTexture(gl.TEXTURE_2D, glTexture);
	gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, frame);

	// Draw
	gl.drawArrays(gl.TRIANGLES, 0, 6);
}
// ========================

// ===== H.264 NAL Unit Parsing =====
function parseNALUnits(data) {
	const nalus = [];
	let offset = 0;

	// Look for start codes (0x000001 or 0x00000001)
	while (offset < data.length - 4) {
		let startCodeLen = 0;

		if (data[offset] === 0 && data[offset + 1] === 0) {
			if (data[offset + 2] === 1) {
				startCodeLen = 3;
			} else if (data[offset + 2] === 0 && data[offset + 3] === 1) {
				startCodeLen = 4;
			}
		}

		if (startCodeLen > 0) {
			// Find next start code
			let nextOffset = offset + startCodeLen;
			while (nextOffset < data.length - 4) {
				if (
					data[nextOffset] === 0 &&
					data[nextOffset + 1] === 0 &&
					(data[nextOffset + 2] === 1 || (data[nextOffset + 2] === 0 && data[nextOffset + 3] === 1))
				) {
					break;
				}
				nextOffset++;
			}

			if (nextOffset >= data.length - 4) {
				nextOffset = data.length;
			}

			nalus.push(data.slice(offset, nextOffset));
			offset = nextOffset;
		} else {
			offset++;
		}
	}

	// Handle case where data is a single NAL without start code
	if (nalus.length === 0 && data.length > 0) {
		// Add start code prefix
		const withStartCode = new Uint8Array(4 + data.length);
		withStartCode[0] = 0;
		withStartCode[1] = 0;
		withStartCode[2] = 0;
		withStartCode[3] = 1;
		withStartCode.set(data, 4);
		nalus.push(withStartCode);
	}

	return nalus;
}

function getNALUType(nalu) {
	// Skip start code
	let offset = 0;
	if (nalu[0] === 0 && nalu[1] === 0) {
		offset = nalu[2] === 1 ? 3 : 4;
	}
	return nalu[offset] & 0x1f;
}

function parseSPSForCodecString(spsData) {
	// Skip start code
	let offset = 0;
	if (spsData[0] === 0 && spsData[1] === 0) {
		offset = spsData[2] === 1 ? 3 : 4;
	}

	const profileIdc = spsData[offset + 1];
	const constraintFlags = spsData[offset + 2];
	const levelIdc = spsData[offset + 3];

	const hex = (n) => n.toString(16).padStart(2, '0');
	return `avc1.${hex(profileIdc)}${hex(constraintFlags)}${hex(levelIdc)}`;
}

// Convert NALUs from Annex B (start codes) to AVCC format (length prefixed)
function naluToAvcc(naluWithStartCode) {
	// Skip start code to get raw NAL data
	let offset = 0;
	if (naluWithStartCode[0] === 0 && naluWithStartCode[1] === 0) {
		offset = naluWithStartCode[2] === 1 ? 3 : 4;
	}
	const rawNalu = naluWithStartCode.slice(offset);

	// Create AVCC format: 4-byte length prefix + NAL data
	const avcc = new Uint8Array(4 + rawNalu.length);
	const len = rawNalu.length;
	avcc[0] = (len >> 24) & 0xff;
	avcc[1] = (len >> 16) & 0xff;
	avcc[2] = (len >> 8) & 0xff;
	avcc[3] = len & 0xff;
	avcc.set(rawNalu, 4);
	return avcc;
}

function configureVideoDecoder(spsNalu, ppsNalu) {
	const codecString = parseSPSForCodecString(spsNalu);
	console.log('[Worker] Configuring decoder with codec:', codecString);

	// Skip start codes from SPS/PPS
	let spsOffset = spsNalu[2] === 1 ? 3 : 4;
	let ppsOffset = ppsNalu[2] === 1 ? 3 : 4;
	const spsRaw = spsNalu.slice(spsOffset);
	const ppsRaw = ppsNalu.slice(ppsOffset);

	// Create avcC box for decoder config
	const avcC = new Uint8Array([
		0x01, // configurationVersion
		spsRaw[1], // AVCProfileIndication
		spsRaw[2], // profile_compatibility
		spsRaw[3], // AVCLevelIndication
		0xff, // lengthSizeMinusOne (3 = 4 bytes)
		0xe1, // numOfSequenceParameterSets (1)
		(spsRaw.length >> 8) & 0xff,
		spsRaw.length & 0xff,
		...spsRaw,
		0x01, // numOfPictureParameterSets (1)
		(ppsRaw.length >> 8) & 0xff,
		ppsRaw.length & 0xff,
		...ppsRaw,
	]);

	videoDecoder.configure({
		codec: codecString,
		codedWidth: 1280,
		codedHeight: 720,
		description: avcC,
		hardwareAcceleration: 'prefer-hardware',
		optimizeForLatency: true,
	});

	codecConfig = { sps: spsNalu, pps: ppsNalu };
	console.log('[Worker] Decoder configured');
}

function processH264Frame(data, timestamp) {
	if (!videoDecoder || videoDecoder.state === 'closed') return;

	const nalus = parseNALUnits(data);

	for (const nalu of nalus) {
		const naluType = getNALUType(nalu);

		// 7 = SPS, 8 = PPS, 5 = IDR, 1 = non-IDR
		if (naluType === 7) {
			h264Sps = nalu;
		} else if (naluType === 8) {
			h264Pps = nalu;
			if (h264Sps && h264Pps && !codecConfig) {
				configureVideoDecoder(h264Sps, h264Pps);
			}
		} else if (naluType === 5 || naluType === 1) {
			if (videoDecoder.state !== 'configured') {
				return;
			}

			const isKeyFrame = naluType === 5;

			// Build frame data in AVCC format
			let frameData;
			if (isKeyFrame && h264Sps && h264Pps) {
				// For keyframes, prepend SPS and PPS
				const spsAvcc = naluToAvcc(h264Sps);
				const ppsAvcc = naluToAvcc(h264Pps);
				const idrAvcc = naluToAvcc(nalu);

				frameData = new Uint8Array(spsAvcc.length + ppsAvcc.length + idrAvcc.length);
				frameData.set(spsAvcc, 0);
				frameData.set(ppsAvcc, spsAvcc.length);
				frameData.set(idrAvcc, spsAvcc.length + ppsAvcc.length);
			} else {
				frameData = naluToAvcc(nalu);
			}

			const chunk = new EncodedVideoChunk({
				type: isKeyFrame ? 'key' : 'delta',
				timestamp: timestamp,
				data: frameData,
			});

			try {
				videoDecoder.decode(chunk);
			} catch (e) {
				console.error('[Worker] Decode error:', e);
			}
		}
	}
}

function createVideoDecoder() {
	return new VideoDecoder({
		output: (frame) => {
			const now = performance.now();
			lastFrameTime = now;

			// Render frame using WebGL
			renderFrame(frame);
			frame.close();

			frameCount++;

			// Notify main thread of first frame and stats
			self.postMessage({
				type: 'frame',
				frameCount: frameCount,
				timestamp: now,
			});
		},
		error: (e) => {
			console.error('[Worker] Decoder error:', e);
			self.postMessage({ type: 'error', message: e.message });
		},
	});
}

function reset() {
	if (videoDecoder && videoDecoder.state !== 'closed') {
		try {
			videoDecoder.close();
		} catch (e) {}
	}
	videoDecoder = null;
	codecConfig = null;
	h264Sps = null;
	h264Pps = null;
	frameCount = 0;
	lastFrameTime = 0;
	// Note: WebGL context and resources are preserved across reset
}

// Handle messages from main thread
self.onmessage = (event) => {
	const { type, data } = event.data;

	switch (type) {
		case 'init':
			// Receive the OffscreenCanvas and init WebGL
			canvas = event.data.canvas;
			if (!initWebGL(canvas)) {
				self.postMessage({ type: 'error', message: 'WebGL initialization failed' });
				return;
			}
			videoDecoder = createVideoDecoder();
			console.log('[Worker] Initialized with OffscreenCanvas + WebGL');
			self.postMessage({ type: 'ready' });
			break;

		case 'frame':
			// Process encoded H.264 frame
			processH264Frame(new Uint8Array(data.buffer), data.timestamp);
			break;

		case 'reset':
			reset();
			videoDecoder = createVideoDecoder();
			console.log('[Worker] Reset decoder');
			break;

		case 'close':
			reset();
			console.log('[Worker] Closed');
			break;

		case 'getStats':
			self.postMessage({
				type: 'stats',
				frameCount: frameCount,
				lastFrameTime: lastFrameTime,
				decoderState: videoDecoder ? videoDecoder.state : 'none',
				queueSize: videoDecoder ? videoDecoder.decodeQueueSize || 0 : 0,
				width: videoWidth,
				height: videoHeight,
			});
			break;
	}
};
