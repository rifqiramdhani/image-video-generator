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
import textwrap

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
    transcript = data.get("transcript", "").strip()

    # validasi
    if not images or not audio_url or not bgm_url or not transcript:
        return jsonify({"error": "Harus menyediakan image_urls, audio_url, bgm_url, dan transcript"}), 400

    # download semua file
    image_paths = []
    for url in images:
        ext = os.path.splitext(url)[1] or ".jpg"
        path = f"/tmp/{uuid.uuid4().hex}{ext}"
        download_file(url, path)
        image_paths.append(path)

    audio_path  = f"/tmp/{uuid.uuid4().hex}.mp3"
    bgm_path    = f"/tmp/{uuid.uuid4().hex}.mp3"
    srt_path    = f"/tmp/{uuid.uuid4().hex}.srt"
    output_path = f"/tmp/{uuid.uuid4().hex}.mp4"
    download_file(audio_url, audio_path)
    download_file(bgm_url, bgm_path)

    try:
        # 1. hitung total durasi audio
        total_dur = get_audio_duration(audio_path)

        # 2. split transcript jadi paragraf
        paras = [p.strip() for p in transcript.split("\n\n") if p.strip()]
        # 3. hitung durasi tiap paragraf proporsional
        lengths = [len(p) for p in paras]
        total_chars = sum(lengths)
        times = [ total_dur * (l/total_chars) for l in lengths ]

        # 4. buat file SRT
        def fmt_time(sec):
            hrs = int(sec//3600)
            mins = int((sec%3600)//60)
            secs = int(sec%60)
            ms = int((sec - int(sec))*1000)
            return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

        with open(srt_path, "w") as sf:
            cursor = 0.0
            idx = 1
            for p, dur in zip(paras, times):
                # wrap ke maksimal 40 karakter per baris (garis panjang bisa disesuaikan)
                lines = textwrap.wrap(p, width=40)
                # bikin entry berisi 2 baris per blok
                for i in range(0, len(lines), 2):
                    chunk = lines[i:i+2]
                    start = cursor
                    # durasi tiap blok proporsional: total durasi paragraf dibagi jumlah blok
                    block_dur = dur * (len(chunk) / len(lines))
                    end = start + block_dur

                    # format waktu
                    def fmt_time(sec):
                        hrs = int(sec//3600)
                        mins = int((sec%3600)//60)
                        secs = int(sec%60)
                        ms = int((sec - int(sec))*1000)
                        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

                    sf.write(f"{idx}\n")
                    sf.write(f"{fmt_time(start)} --> {fmt_time(end)}\n")
                    sf.write("\n".join(chunk) + "\n\n")

                    cursor = end
                    idx += 1

        # 5. build ffmpeg inputs & filter_complex dinamis
        per_times = times
        num_imgs  = len(image_paths)
        audio_idx = num_imgs
        bgm_idx   = num_imgs + 1

        # inputs
        inputs = ""
        for idx, (img, t) in enumerate(zip(image_paths, per_times)):
            inputs += f"-loop 1 -t {t:.3f} -i '{img}' "
        inputs += f"-i '{audio_path}' -i '{bgm_path}' "

        # video filters
        filters = [
            f"[{i}:v]scale=1920:1920:force_original_aspect_ratio=decrease,"
            f"crop=1920:1080,setsar=1[v{i}]"
            for i in range(num_imgs)
        ]
        concat_lbl = "".join(f"[v{i}]" for i in range(num_imgs))
        filters.append(f"{concat_lbl}concat=n={num_imgs}:v=1:a=0,subtitles='{srt_path}'[vout]")
        # audio mix
        filters.append(
            f"[{bgm_idx}:a]volume=0.4[bgm];"
            f"[{audio_idx}:a][bgm]amix=inputs=2:duration=first[aout]"
        )

        filter_complex = ";".join(filters)

        # 6. build & run ffmpeg
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

        # cleanup
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
