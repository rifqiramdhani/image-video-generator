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
        metadata = parse_imagemagick_verbose_output(result.stdout)

        return jsonify(metadata)

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
    finally:
        # Pastikan file temporary dihapus, bahkan jika ada kesalahan
        if os.path.exists(temp_image_path):
            os.remove(temp_image_path)


def parse_imagemagick_verbose_output(output_string):
    """
    Parses the verbose output of ImageMagick's identify command into a dictionary.
    This is a simplified parser and might need adjustments based on the exact
    metadata you wish to extract and its variability.
    """
    metadata = {}
    current_section = None

    for line in output_string.splitlines():
        line = line.strip()

        # Check for main sections (e.g., "Image:", "Properties:", "EXIF:")
        if line.startswith("Image:"):
            current_section = "Image"
            metadata["Image"] = {}
        elif line.startswith("Properties:"):
            current_section = "Properties"
            metadata["Properties"] = {}
        elif line.startswith("EXIF:"):
            current_section = "EXIF"
            metadata["EXIF"] = {}
        elif line.startswith("IPTC:"):
            current_section = "IPTC"
            metadata["IPTC"] = {}
        elif line.startswith("Channel statistics:"):
            current_section = "Channel statistics"
            metadata["Channel statistics"] = {}
        # Add more sections as needed (e.g., "ICC profile:", "Profile-xmp:")

        # Parse key-value pairs within sections
        if current_section and ":" in line and not line.startswith(" "): # Avoid sub-sections with leading spaces
            key_value = line.split(":", 1) # Split only on the first colon
            key = key_value[0].strip()
            value = key_value[1].strip()
            if current_section == "Image" and key in ["Format", "Geometry", "Mime type", "Colorspace", "Depth", "Resolution"]:
                metadata[current_section][key] = value
            elif current_section in ["Properties", "EXIF", "IPTC"] and key:
                metadata[current_section][key] = value
            # Handle special cases like Resolution which might have "x"
            if key == "Resolution":
                try:
                    res_parts = value.split("x")
                    if len(res_parts) == 2:
                        metadata[current_section]["ResolutionX"] = res_parts[0]
                        metadata[current_section]["ResolutionY"] = res_parts[1]
                except:
                    pass # Ignore if parsing fails

    # Flatten some common top-level metadata for easier access if desired
    # For example, to get direct access to width, height, format
    if "Image" in metadata:
        if "Geometry" in metadata["Image"]:
            geo = metadata["Image"]["Geometry"].split('+')[0] # Remove potential +0+0
            metadata["width"], metadata["height"] = geo.split('x')
        if "Format" in metadata["Image"]:
            metadata["format"] = metadata["Image"]["Format"]
        if "Mime type" in metadata["Image"]:
            metadata["mime_type"] = metadata["Image"]["Mime type"]
        if "Colorspace" in metadata["Image"]:
            metadata["colorspace"] = metadata["Image"]["Colorspace"]
        if "Depth" in metadata["Image"]:
            metadata["depth"] = metadata["Image"]["Depth"]

    # Add example of specific EXIF extraction
    if "EXIF" in metadata:
        if "DateTimeOriginal" in metadata["EXIF"]:
            metadata["creation_date_from_exif"] = metadata["EXIF"]["DateTimeOriginal"]
        if "Make" in metadata["EXIF"]:
            metadata["camera_make"] = metadata["EXIF"]["Make"]
        if "Model" in metadata["EXIF"]:
            metadata["camera_model"] = metadata["EXIF"]["Model"]

    return metadata

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int("5000"),
        debug=True,
        use_reloader=True
    )
