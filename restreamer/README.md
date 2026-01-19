# YouTube Restreamer

Restreams the RC car's WebRTC video feed to YouTube Live via RTMP.

## How it works

```
Pi (car) → WHEP → Cloudflare Tunnel → [This service] → RTMP → YouTube Live
                                            ↓
                              MediaMTX consumes WHEP
                              FFmpeg re-encodes to RTMP
```

## Deploy to Fly.io

```bash
cd restreamer

# Create the app (first time only)
fly launch --no-deploy

# Set secrets
fly secrets set CAM_WHEP_URL="https://cam.yourdomain.com/cam/whep"
fly secrets set YOUTUBE_STREAM_KEY="xxxx-xxxx-xxxx-xxxx"
fly secrets set CONTROL_SECRET="your-secret-here"

# Deploy
fly deploy
```

## Usage

The service scales to zero when not in use. Control it via HTTP:

```bash
RESTREAMER_URL="https://{your-app}.fly.dev"
SECRET="your-secret-here"

# Check status
curl $RESTREAMER_URL/status

# Start streaming to YouTube
curl -X POST -H "Authorization: Bearer $SECRET" $RESTREAMER_URL/start

# Stop streaming
curl -X POST -H "Authorization: Bearer $SECRET" $RESTREAMER_URL/stop

# Health check (keeps machine alive)
curl $RESTREAMER_URL/health
```

## Add to Admin Dashboard

You can add buttons to the admin page to control the restreamer:

```javascript
async function startYouTubeStream() {
  await fetch("https://{your-app}.fly.dev/start", {
    method: "POST",
    headers: { Authorization: "Bearer " + RESTREAMER_SECRET },
  });
}
```

## Configuration

| Variable             | Description                                                        |
| -------------------- | ------------------------------------------------------------------ |
| `CAM_WHEP_URL`       | WHEP endpoint for car camera                                       |
| `YOUTUBE_RTMP_URL`   | YouTube RTMP ingest URL (default: rtmp://a.rtmp.youtube.com/live2) |
| `YOUTUBE_STREAM_KEY` | Your YouTube stream key                                            |
| `CONTROL_SECRET`     | Secret for start/stop authentication                               |

## Costs

- Fly.io free tier includes enough for occasional use
- With auto_stop, you only pay when streaming
- ~$0.50-2/month for occasional 1-2 hour streams
