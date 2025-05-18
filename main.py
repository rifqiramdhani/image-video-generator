import os
import uuid
import threading
import logging
import requests
import gdown
import openai
from flask import Flask, request, send_file, jsonify

openai.api_key = os.getenv("OPENAI_API_KEY")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

def cleanup_files(*paths):
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

def download_file(url: str, path: str):
    if "drive.google.com" in url:
        gdown.download(url, path, quiet=True)
        if not os.path.exists(path):
            raise RuntimeError("gdown failed")
    else:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)

@app.route("/generate-video", methods=["POST"])
def generate_video():
    data = request.get_json()
    image_url = data["image_url"]
    audio_url = data["audio_url"]
    bgm_url   = data["bgm_url"]

    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    image_url = data.get("image_url")
    audio_url = data.get("audio_url")
    bgm_url   = data.get("bgm_url")

    if not image_url or not audio_url or not bgm_url:
        return jsonify({"error": "Missing one of image_url, audio_url, or bgm_url"}), 400

    image_path  = f"/tmp/{uuid.uuid4().hex}.jpg"
    audio_path  = f"/tmp/{uuid.uuid4().hex}.mp3"
    bgm_path    = f"/tmp/{uuid.uuid4().hex}.mp3"
    srt_path    = f"/tmp/{uuid.uuid4().hex}.srt"
    output_path = f"/tmp/{uuid.uuid4().hex}.mp4"

    try:
        download_file(image_url, image_path)
        download_file(audio_url, audio_path)
        download_file(bgm_url, bgm_path)

        with open(audio_path, "rb") as af:
            transcript = openai.Audio.transcribe(
                model="whisper-1",
                file=af,
                response_format="srt"
            )
        with open(srt_path, "w") as sf:
            sf.write(transcript)

        cmd = (
            f"ffmpeg -y "
            f"-loop 1 -i '{image_path}' "
            f"-i '{audio_path}' "
            f"-i '{bgm_path}' "
            f"-filter_complex \""
            f"[0:v]scale=1920:1920:force_original_aspect_ratio=decrease,crop=1920:1080,subtitles='{srt_path}'[vout];"
            f"[2:a]volume=0.4[bgm];"
            f"[1:a][bgm]amix=inputs=2:duration=first[aout]\" "
            f"-map '[vout]' -map '[aout]' "
            f"-c:v libx264 -pix_fmt yuv420p "
            f"-c:a aac -b:a 192k -shortest '{output_path}'"
        )
        logger.info("Running ffmpeg command")
        if os.system(cmd) != 0:
            raise RuntimeError("ffmpeg failed to")

        # schedule cleanup in 60s
        threading.Timer(60, cleanup_files, args=[image_path, audio_path, bgm_path, srt_path, output_path]).start()

        return send_file(output_path, mimetype="video/mp4", as_attachment=True, attachment_filename="output.mp4")

    except Exception as e:
        logger.exception("Error generating video ")
        cleanup_files(image_path, audio_path, bgm_path, srt_path, output_path)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8888)),
        debug=True,
        use_reloader=True
    )
