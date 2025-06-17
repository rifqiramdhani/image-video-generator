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
        metadata = parse_imagemagick_verbose_output_unfiltered(result.stdout)

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


def parse_imagemagick_verbose_output_unfiltered(output_string):
    """
    Parses the verbose output of ImageMagick's identify command into a dictionary,
    attempting to capture all information, including nested sections like statistics.
    """
    metadata = {}
    current_main_section = None
    current_sub_section = None
    current_sub_sub_section = None # For deeply nested parts like Red:, Green:, Blue:

    lines = output_string.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Check for main sections (e.g., "Image:", "Properties:", "EXIF:", "Channel statistics:")
        if line.endswith(":"):
            # A main section or a top-level subsection (like "Image statistics:")
            section_name = line[:-1].strip() # Remove trailing colon

            if section_name == "Image":
                current_main_section = "Image"
                metadata["Image"] = {}
                current_sub_section = None
                current_sub_sub_section = None
            elif section_name == "Properties":
                current_main_section = "Properties"
                metadata["Properties"] = {}
                current_sub_section = None
                current_sub_sub_section = None
            elif section_name == "EXIF":
                current_main_section = "EXIF"
                metadata["EXIF"] = {}
                current_sub_section = None
                current_sub_sub_section = None
            elif section_name == "IPTC":
                current_main_section = "IPTC"
                metadata["IPTC"] = {}
                current_sub_section = None
                current_sub_sub_section = None
            elif section_name == "Channel statistics":
                current_main_section = "Channel statistics"
                metadata["Channel statistics"] = {}
                current_sub_section = None
                current_sub_sub_section = None
            elif section_name == "Image statistics":
                current_main_section = "Image statistics"
                metadata["Image statistics"] = {}
                current_sub_section = None
                current_sub_sub_section = None
            elif section_name == "Chromaticity":
                current_main_section = "Chromaticity"
                metadata["Chromaticity"] = {}
                current_sub_section = None
                current_sub_sub_section = None
            # Add other main sections here as they appear (e.g., "ICC profile:", "Profile-xmp:")
            else:
                # This might be a subsection within a main section, or a new unhandled section
                # Try to add it to the current_main_section if exists, or as a new top-level
                if current_main_section and section_name not in ["Channel depth"]: # Channel depth is handled below
                    if current_main_section not in metadata:
                        metadata[current_main_section] = {}
                    metadata[current_main_section][section_name] = {}
                    current_sub_section = section_name
                    current_sub_sub_section = None
                else:
                    # If it's a completely new section not yet classified
                    metadata[section_name] = {}
                    current_main_section = section_name
                    current_sub_section = None
                    current_sub_sub_section = None
        
        # Check for Channel depth: (special handling due to direct key-value pairs)
        elif "Channel depth:" in line:
            key_value = line.split(":", 1)
            if len(key_value) == 2 and current_main_section == "Image":
                metadata["Image"]["Channel depth"] = {}
                current_sub_section = "Channel depth"
                current_sub_sub_section = None # Reset sub-sub-section

        # Process key-value pairs
        elif ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip()
            value = parts[1].strip()

            if current_main_section == "Image":
                if current_sub_section == "Channel depth":
                    metadata["Image"]["Channel depth"][key] = value
                elif current_main_section == "Image" and current_sub_section is None:
                    # Direct Image properties
                    metadata["Image"][key] = value
            elif current_main_section in ["Properties", "EXIF", "IPTC", "Artifacts"]:
                if current_main_section not in metadata:
                    metadata[current_main_section] = {} # Safety check
                metadata[current_main_section][key] = value
            elif current_main_section == "Chromaticity":
                if current_main_section not in metadata:
                    metadata[current_main_section] = {} # Safety check
                metadata[current_main_section][key] = value
            elif current_main_section in ["Channel statistics", "Image statistics"]:
                # Handle nested statistics: Overall:, Red:, Green:, Blue:
                if key in ["Pixels", "Red", "Green", "Blue", "Overall"]: # These are subsections within statistics
                    if current_main_section not in metadata:
                        metadata[current_main_section] = {} # Safety check
                    metadata[current_main_section][key] = {}
                    current_sub_sub_section = key # Set this as the active sub-sub-section
                    # If it's a key like "Pixels", it's a direct value, not a subsection
                    if key == "Pixels":
                        metadata[current_main_section][key] = value
                        current_sub_sub_section = None # Reset if it's a direct value
                elif current_sub_sub_section:
                    # Properties within a statistics channel (min, max, mean, etc.)
                    if current_main_section not in metadata:
                        metadata[current_main_section] = {} # Safety check
                    if current_sub_sub_section not in metadata[current_main_section]:
                        metadata[current_main_section][current_sub_sub_section] = {} # Safety check
                    metadata[current_main_section][current_sub_sub_section][key] = value
                else:
                    # Fallback for unexpected direct keys in statistics section
                    if current_main_section not in metadata:
                        metadata[current_main_section] = {} # Safety check
                    metadata[current_main_section][key] = value
            else:
                # Top-level properties (like Filesize, Number pixels, User time, Version)
                # These are usually at the very end and don't belong to a preceding section.
                # Only add if it's not part of another recognized structure.
                if current_main_section not in metadata and not any(line.startswith(s + ":") for s in ["Image", "Properties", "EXIF", "IPTC", "Channel statistics", "Image statistics", "Chromaticity", "Artifacts"]):
                    metadata[key] = value

        i += 1 # Move to the next line

    # Post-processing for fields like Geometry, Filesize etc. if needed
    # You can add specific parsing for 'Geometry', 'Filesize', 'Version' etc.
    # to convert them to more usable types (e.g., int, float) if they are string.
    # For example:
    if 'Image' in metadata and 'Geometry' in metadata['Image']:
        try:
            geom_parts = metadata['Image']['Geometry'].split('+')[0].split('x')
            metadata['Image']['width'] = int(geom_parts[0])
            metadata['Image']['height'] = int(geom_parts[1])
        except (ValueError, IndexError):
            pass # Keep as string if parsing fails
    
    if 'Filesize' in metadata:
        try:
            # Remove 'B' or 'KB' or 'MB' and convert to int/float
            size_str = metadata['Filesize'].replace('B', '').replace('K', '*1024').replace('M', '*1024*1024')
            if '*' in size_str:
                metadata['Filesize_bytes'] = eval(size_str) # Use eval for basic calculations like 164674B
            else:
                metadata['Filesize_bytes'] = int(size_str)
        except Exception:
            pass # Keep original string if parsing fails


    return metadata


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int("5000"),
        debug=True,
        use_reloader=True
    )
