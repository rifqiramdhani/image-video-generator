import os
import uuid
import threading
import logging
import requests
import gdown
import openai
import subprocess
from typing import List
from flask import Flask, request, send_file, jsonify
from dotenv import load_dotenv

load_dotenv()
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

def get_audio_duration(path: str) -> float:
    # Call ffprobe to get duration in seconds
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    return float(out.strip())

@app.route('/')
def index():
    return jsonify({"Choo Choo": "Welcome to your Flask app 🚅"})

@app.route("/generate-video", methods=["POST"])
def generate_video():
    data = request.get_json() or {}
    images: List[str] = data.get("image_urls") or []
    audio_url = data.get("audio_url")
    bgm_url   = data.get("bgm_url")

    if not images or not audio_url or not bgm_url:
        return jsonify({"error": "Must provide image_urls (list), audio_url, and bgm_url"}), 400

    # prepare temp paths
    image_paths = []
    for url in images:
        path = f"/tmp/{uuid.uuid4().hex}{os.path.splitext(url)[1] or '.jpg'}"
        download_file(url, path)
        image_paths.append(path)

    audio_path  = f"/tmp/{uuid.uuid4().hex}.mp3"
    bgm_path    = f"/tmp/{uuid.uuid4().hex}.mp3"
    srt_path    = f"/tmp/{uuid.uuid4().hex}.srt"
    output_path = f"/tmp/{uuid.uuid4().hex}.mp4"

    try:
        download_file(audio_url, audio_path)
        download_file(bgm_url, bgm_path)

        # Whisper → SRT
        with open(audio_path, "rb") as af:
            transcript = openai.audio.transcribe(
                model="whisper-1",
                file=af,
                response_format="srt"
            )
        with open(srt_path, "w") as sf:
            sf.write(transcript)

        # figure out total audio duration
        total_dur = get_audio_duration(audio_path)
        per_image = total_dur / len(image_paths)

        # build ffmpeg inputs and filters
        inputs = ""
        filters = []
        for idx, img in enumerate(image_paths):
            inputs += f"-loop 1 -t {per_image:.3f} -i '{img}' "
            filters.append(f"[{idx}:v]scale=1920:1920:force_original_aspect_ratio=decrease,"
                           f"crop=1920:1080,setsar=1[v{idx}]")

        # concat filter for all image streams
        concat_inputs = "".join(f"[v{idx}]" for idx in range(len(image_paths)))
        filters.append(f"{concat_inputs}concat=n={len(image_paths)}:v=1:a=0,subtitles='{srt_path}'[vout]")

        # background music volume + mix
        inputs += f"-i '{audio_path}' -i '{bgm_path}' "
        filters.append("[3:a]volume=0.4[bgm];[2:a][bgm]amix=inputs=2:duration=first[aout]")

        filter_complex = ";".join(filters)

        cmd = (
            f"ffmpeg -y {inputs}"
            f"-filter_complex \"{filter_complex}\" "
            f"-map \"[vout]\" -map \"[aout]\" "
            f"-c:v libx264 -pix_fmt yuv420p "
            f"-c:a aac -b:a 192k -shortest '{output_path}'"
        )

        logger.info("Running ffmpeg:\n%s", cmd)
        if os.system(cmd) != 0:
            raise RuntimeError("ffmpeg failed")

        # cleanup later
        threading.Timer(60, cleanup_files,
                        args=tuple(image_paths) + (audio_path, bgm_path, srt_path, output_path)
                       ).start()

        return send_file(output_path, mimetype="video/mp4",
                         as_attachment=True, attachment_filename="output.mp4")

    except Exception as e:
        logger.exception("Error generating video")
        cleanup_files(*(image_paths + [audio_path, bgm_path, srt_path, output_path]))
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0",
            port=int(os.getenv("PORT", 8888)),
            debug=True, use_reloader=True)
