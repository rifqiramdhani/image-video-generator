import os
import uuid
import threading
import logging
import requests
import gdown
import openai
from flask import Flask, request, send_file, jsonify
from dotenv import load_dotenv
import re
from datetime import timedelta
import subprocess
import json
import urllib.request
import urllib.error
import io
from tempfile import NamedTemporaryFile
from datetime import datetime

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

def parse_srt_timings(srt_path):
    pattern = r"(\d{2}):(\d{2}):(\d{2}),\d{3} --> (\d{2}):(\d{2}):(\d{2}),\d{3}"
    timings = []
    with open(srt_path) as f:
        for line in f:
            m = re.match(pattern, line)
            if m:
                h1,m1,s1,h2,m2,s2 = map(int, m.groups())
                start = timedelta(hours=h1, minutes=m1, seconds=s1).total_seconds()
                end   = timedelta(hours=h2, minutes=m2, seconds=s2).total_seconds()
                timings.append(end - start)
    return timings

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

@app.route('/')
def index():
    return jsonify({"Choo Choo": "Welcome to your Flask app 🚅"})

@app.route("/generate-video", methods=["POST"])
def generate_video():
    data = request.get_json()

    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    image_urls = data.get("image_urls")  # list
    audio_url = data["audio_url"]
    bgm_url   = data["bgm_url"]

    if not image_urls or not audio_url or not bgm_url:
        return jsonify({"error": "Missing one of image_urls, audio_url, or bgm_url"}), 400

    image_path  = f"/tmp/{uuid.uuid4().hex}.png"
    audio_path  = f"/tmp/{uuid.uuid4().hex}.mp3"
    bgm_path    = f"/tmp/{uuid.uuid4().hex}.mp3"
    srt_path    = f"/tmp/{uuid.uuid4().hex}.srt"
    output_path = f"/tmp/{uuid.uuid4().hex}.mp4"

    try:
        # 1. Download files
        image_paths = [f"/tmp/{uuid.uuid4().hex}.png" for _ in image_urls]
        for url, path in zip(image_urls, image_paths):
            download_file(url, path)
        download_file(audio_url, audio_path := f"/tmp/{uuid.uuid4().hex}.mp3")
        download_file(bgm_url,   bgm_path   := f"/tmp/{uuid.uuid4().hex}.mp3")

        # 2. Transkripsi → SRT
        transcript = openai.Audio.transcribe("whisper-1", open(audio_path,"rb"), response_format="srt")
        with open(srt_path := f"/tmp/{uuid.uuid4().hex}.srt", "w") as sf:
            sf.write(transcript)

        # 3. Parse durasi tiap segmen subtitle
        # timings = parse_srt_timings(srt_path)
        # Jika jumlah gambar < segmen, bisa: cycle gambar atau pakai durasi rata
        out = subprocess.check_output(
        ["ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1", audio_path]
        )
        total_audio = float(out)
        # bagi rata
        dur_per_image = total_audio / len(image_paths)
        timings = [dur_per_image]*len(image_paths)

        # 4. Buat video segmen
        segment_paths = []
        for idx, (img, dur) in enumerate(zip(image_paths, timings)):
            seg = f"/tmp/seg_{idx}.mp4"
            cmd = (
                f"ffmpeg -y -loop 1 -i '{img}' -t {dur:.3f} "
                f"-vf \"scale=1920:1920:force_original_aspect_ratio=decrease,crop=1920:1080\" "
                f"-c:v libx264 -pix_fmt yuv420p '{seg}'"
            )
            os.system(cmd)
            segment_paths.append(seg)

        # 5. Concat segmen
        concat_list = f"/tmp/{uuid.uuid4().hex}_list.txt"
        with open(concat_list, "w") as f:
            for seg in segment_paths:
                f.write(f"file '{seg}'\n")
        slideshow = f"/tmp/{uuid.uuid4().hex}_slideshow.mp4"
        os.system(f"ffmpeg -y -f concat -safe 0 -i '{concat_list}' -c copy '{slideshow}'")

        # 6. Gabung semua
        cmd = (
            f"ffmpeg -y -i '{slideshow}' -i '{audio_path}' -i '{bgm_path}' "
            f"-filter_complex \"[2:a]volume=0.6[bgm];"
            f"[1:a][bgm]amix=inputs=2:duration=longest[aout];"
            f"[0:v]subtitles='{srt_path}'[vout]\" "
            f"-map '[vout]' -map '[aout]' "
            f"-c:v libx264 -pix_fmt yuv420p "
            f"-c:a aac -b:a 192k "
            f"-t '{total_audio}' '{output_path}'"
        )
        print('Syntax CMD ===== ',cmd)
        logger.info("Running ffmpeg command")
        if os.system(cmd) != 0:
            raise RuntimeError("ffmpeg failed to")

        # schedule cleanup in 60s
        threading.Timer(60, cleanup_files, args=[image_path, audio_path, bgm_path, srt_path, output_path]).start()

        return send_file(output_path, mimetype="video/mp4", as_attachment=True, attachment_filename="output.mp4")

    except Exception as e:
        logger.exception("Error generating video")
        cleanup_files(image_path, audio_path, bgm_path, srt_path, output_path)
        return jsonify({"error": str(e)}), 500

@app.route("/extract-metadata-image", methods=["GET"])
def extract_metadata_image():
    image_url = request.args.get("image_url")

    if not image_url:
        return jsonify({"error": "Parameter 'image_url' tidak ditemukan"}), 400

    # Gunakan ekstensi file asli jika memungkinkan, atau default ke .tmp
    # Ini membantu identify dalam mendeteksi format yang benar
    file_extension = os.path.splitext(image_url)[1] if os.path.splitext(image_url)[1] else ".tmp"
    temp_filename = f"{uuid.uuid4().hex}{file_extension}"
    temp_image_path = os.path.join("/tmp", temp_filename)

    try:
        # Download image
        with urllib.request.urlopen(image_url, timeout=30) as response, open(temp_image_path, "wb") as f_out:
            f_out.write(response.read())

        # Gunakan 'magick identify -verbose' untuk mendapatkan metadata gambar
        # 'magick' adalah perintah standar untuk ImageMagick 7+
        # Jika Anda menggunakan ImageMagick 6, mungkin hanya 'identify'
        command = ['identify', '-verbose', temp_image_path]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True
        )

        # Parse output dari identify -verbose
        # Output ini sangat verbose, jadi kita perlu memparsingnya
        # menjadi struktur data yang lebih terorganisir
        raw_metadata_string = result.stdout

        # Atau, Anda bisa mengembalikan sebagai respons teks biasa jika endpoint ini tidak harus JSON.
        return jsonify({"metadata": raw_metadata_string})

    except urllib.error.URLError as e:
        return jsonify({"error": f"Gagal mengunduh gambar: {e.reason}"}), 500
    except FileNotFoundError:
        # Menangkap error jika 'magick' atau 'identify' tidak ditemukan
        return jsonify({"error": "Perintah 'magick' (ImageMagick) tidak ditemukan. Pastikan ImageMagick terinstal dan berada di PATH."}), 500
    except subprocess.CalledProcessError as e:
        # Menangkap error jika identify gagal memproses file gambar (misal: file rusak)
        return jsonify({"error": "ImageMagick 'identify' gagal memproses file gambar.", "details": e.stderr.strip()}), 400
    except Exception as e:
        # Tangani kesalahan umum lainnya
        return jsonify({"error": f"Terjadi kesalahan tak terduga: {str(e)}"}), 500

@app.route("/merge-audio-video", methods=["POST"])
def merge():
    audio = request.files["audio"].read()
    video = request.files["video"].read()
    merged = merge_audio_video_ffmpeg(audio, video)
    filename = f"merged-video-{datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4"
    return send_file(merged, mimetype="video/mp4", as_attachment=True, attachment_filename=filename)

def merge_audio_video_ffmpeg(audio_binary, video_binary):
    audio_path = video_path = output_path = None

    try:
        # Save audio to temp file
        with NamedTemporaryFile(delete=False, suffix=".mp3") as audio_file:
            audio_file.write(audio_binary)
            audio_path = audio_file.name

        # Save video to temp file
        with NamedTemporaryFile(delete=False, suffix=".mp4") as video_file:
            video_file.write(video_binary)
            video_path = video_file.name

        # Output temp file
        with NamedTemporaryFile(delete=False, suffix=".mp4") as output_file:
            output_path = output_file.name

        # Run FFmpeg command
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0",   # gunakan video dari input 0
            "-map", "1:a:0",   # gunakan audio dari input 1
            "-c:v", "copy",    # copy video tanpa re-encoding
            "-c:a", "aac",     # encode audio ke AAC
            "-shortest",       # potong jika audio lebih panjang dari video
            output_path
        ]

        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr.decode()}")

        with open(output_path, "rb") as f:
            return io.BytesIO(f.read())
    finally:
        cleanup_files(audio_path, video_path, output_path)

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int("5000"),
        debug=True,
        use_reloader=True
    )
