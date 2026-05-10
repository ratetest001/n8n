from flask import Flask, request, jsonify
import subprocess
import requests
import os
import json
import tempfile
import math
import shutil

app = Flask(__name__)

def download_file(url, dest_path):
    """Download any file from URL to local path"""
    response = requests.get(url, stream=True)
    with open(dest_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest_path


def get_audio_duration(audio_path):
    """Get exact audio duration using ffprobe on local file"""
    result = subprocess.run([
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        audio_path
    ], capture_output=True, text=True)
    
    output = result.stdout.strip()
    
    # Fallback if ffprobe still can't read it
    if not output or output == 'N/A':
        # Estimate from file size: MP3 128kbps = 16000 bytes/sec
        file_size = os.path.getsize(audio_path)
        return max(5.0, file_size / 16000)
    
    return float(output)


def download_file(url, dest_path):
    """Download file following redirects"""
    session = requests.Session()
    response = session.get(url, stream=True, allow_redirects=True)
    
    # Handle Google Drive virus scan warning page
    for key, value in response.cookies.items():
        if key.startswith('download_warning'):
            # Re-request with confirm parameter
            url = url + f"&confirm={value}"
            response = session.get(url, stream=True, allow_redirects=True)
            break
    
    with open(dest_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)
    
    return dest_path


def create_scene_video(scene, temp_dir, scene_index):
    """Create MP4 for one scene"""
    
    # --- Download audio FIRST ---
    audio_id = scene['audioFileId']
    audio_url = f"https://drive.google.com/uc?export=download&id={audio_id}"
    audio_path = os.path.join(temp_dir, f"audio_{scene_index}.mp3")
    print(f"Downloading audio for scene {scene_index}...")
    download_file(audio_url, audio_path)
    
    # --- Verify audio downloaded correctly ---
    audio_size = os.path.getsize(audio_path)
    print(f"Audio size: {audio_size} bytes")
    if audio_size < 1000:
        raise Exception(f"Audio download failed or too small: {audio_size} bytes")
    
    # --- Get exact duration from LOCAL file ---
    duration = get_audio_duration(audio_path)
    print(f"Scene {scene_index} audio duration: {duration}s")
    
    num_images = len(scene['images'])
    time_per_image = duration / num_images
    
    # --- Download images ---
    image_paths = []
    for i, img_url in enumerate(scene['images']):
        img_path = os.path.join(temp_dir, f"img_{scene_index}_{i}.jpg")
        download_file(img_url, img_path)
        image_paths.append(img_path)
    
    # --- Build FFmpeg filter ---
    filter_parts = []
    inputs = []
    
    for i, img_path in enumerate(image_paths):
        inputs.extend(['-loop', '1', '-t', str(time_per_image), '-i', img_path])
        zoom = "zoom+0.001" if i % 2 == 0 else "if(lte(zoom,1.0),1.5,max(1.001,zoom-0.001))"
        filter_parts.append(
            f"[{i}]scale=1920:1080,setsar=1,"
            f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={int(time_per_image*25)}:s=1920x1080:fps=25[v{i}]"
        )
    
    # Concat all image clips
    concat_inputs = ''.join([f'[v{i}]' for i in range(num_images)])
    filter_parts.append(f"{concat_inputs}concat=n={num_images}:v=1:a=0[base]")
    
    # Audio input index
    audio_input_idx = num_images
    inputs.extend(['-i', audio_path])
    
    # Text overlay — escape special chars
    text = scene.get('text', '')
    text = text.replace('\\', '\\\\')
    text = text.replace("'", "\u2019")
    text = text.replace(':', '\\:')
    text = text.replace(',', '\\,')
    
    filter_parts.append(
        f"[base]drawtext="
        f"text='{text}':"
        f"fontsize=38:"
        f"fontcolor=white:"
        f"box=1:boxcolor=black@0.6:boxborderw=12:"
        f"x=(w-text_w)/2:"
        f"y=h-120[outv]"
    )
    
    filter_complex = ';'.join(filter_parts)
    scene_output = os.path.join(temp_dir, f"scene_{scene_index}.mp4")
    
    cmd = ['ffmpeg', '-y']
    cmd.extend(inputs)
    cmd.extend([
        '-filter_complex', filter_complex,
        '-map', '[outv]',
        '-map', f'{audio_input_idx}:a',
        '-c:v', 'libx264',
        '-preset', 'fast',
        '-crf', '23',
        '-c:a', 'aac',
        '-shortest',
        '-r', '25',
        scene_output
    ])
    
    print(f"Running FFmpeg for scene {scene_index}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise Exception(f"FFmpeg error: {result.stderr[-500:]}")
    
    return scene_output

def concatenate_scenes(scene_videos, output_path, temp_dir):
    """Concatenate all scene MP4s into final video"""
    
    # Create concat file
    concat_file = os.path.join(temp_dir, 'concat.txt')
    with open(concat_file, 'w') as f:
        for video_path in scene_videos:
            f.write(f"file '{video_path}'\n")
    
    cmd = [
        'ffmpeg', '-y',
        '-f', 'concat',
        '-safe', '0',
        '-i', concat_file,
        '-c', 'copy',
        output_path
    ]
    subprocess.run(cmd, check=True)
    return output_path

@app.route('/generate-video', methods=['POST'])
def generate_video():
    try:
        data = request.json
        scenes = data

        with tempfile.TemporaryDirectory() as temp_dir:
            scene_videos = []
            for i, scene in enumerate(scenes):
                print(f"Processing scene {i+1}/{len(scenes)}...")
                scene_path = create_scene_video(scene, temp_dir, i)
                scene_videos.append(scene_path)

            final_output = os.path.join(temp_dir, 'final_video.mp4')
            concatenate_scenes(scene_videos, final_output, temp_dir)

            file_size = os.path.getsize(final_output)

            return jsonify({
                "success": True,
                "message": "Video generated successfully",
                "file_size_mb": round(file_size / 1024 / 1024, 2),
                "scenes_processed": len(scenes)
            })

    except Exception as e:
        import traceback
        print(traceback.format_exc())  # Full error in Railway logs
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/health', methods=['GET'])
def health():
    ffmpeg_path = shutil.which('ffmpeg')
    ffprobe_path = shutil.which('ffprobe')
    return jsonify({
        "status": "ok",
        "ffmpeg": ffmpeg_path or "NOT FOUND",
        "ffprobe": ffprobe_path or "NOT FOUND"
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)