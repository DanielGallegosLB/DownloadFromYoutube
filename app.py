# Importamos las bibliotecas necesarias
import os
import shutil
import subprocess
import time
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import yt_dlp
import logging

# Inicializamos el logger para propósitos de depuración
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Inicializamos la aplicación Flask
app = Flask(__name__)
CORS(app)  # Habilitamos CORS para toda la aplicación

# Configuración del backend
# Rutas relativas al directorio actual del proyecto
DOWNLOAD_FOLDER = "downloads"  # La carpeta 'downloads' se creará en el mismo directorio del proyecto
FFMPEG_PATH = os.path.join("ffmpeg", "ffmpeg.exe")

# Aseguramos que la carpeta de descargas exista
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

# Endpoint para servir la página web
@app.route('/')
def index():
    """
    Sirve el archivo HTML principal de la aplicación.
    """
    return render_template('index.html')

# Endpoint para obtener las opciones de descarga
@app.route('/api/get-download-options', methods=['POST'])
def get_download_options():
    """
    Este endpoint recibe una URL de YouTube y devuelve las opciones de descarga
    usando la biblioteca yt-dlp.
    """
    logging.info("Llamada a la API: /api/get-download-options")
    try:
        data = request.json
        video_url = data.get('url')
        logging.info(f"URL recibida: {video_url}")

        if not video_url:
            logging.error("No se proporcionó una URL en la solicitud.")
            return jsonify({"error": "No se proporcionó un URL."}), 400

        # Verificamos que ffmpeg existe en la ruta especificada
        ffmpeg_exists = os.path.exists(FFMPEG_PATH)
        logging.info(f"Verificando FFmpeg en la ruta: {FFMPEG_PATH}. Existe: {ffmpeg_exists}")
        if not ffmpeg_exists:
            logging.error(f"ffmpeg.exe no se encuentra en la ruta especificada: {FFMPEG_PATH}")
            return jsonify({"error": f"Error: No se encontró ffmpeg.exe en la ruta especificada: {FFMPEG_PATH}"}), 500

        # Configuramos yt-dlp para obtener información sin descargar.
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'force_generic_extractor': True,
            'ffmpeg_location': os.path.dirname(FFMPEG_PATH)
        }
        logging.info(f"Opciones de yt-dlp para la extracción de información: {ydl_opts}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(video_url, download=False)
            
            video_title = info_dict.get('title', 'Video')
            
            # Filtramos los formatos para obtener opciones claras
            formats = info_dict.get('formats', [])
            
            progressive_options = []
            adaptive_video_options = []
            audio_only_options = []
            seen_resolutions = set()

            for f in formats:
                # Streams progresivos (video + audio)
                if f.get('ext') == 'mp4' and f.get('acodec') and f.get('vcodec') != 'none':
                    progressive_options.append({
                        'format_id': f.get('format_id'),
                        'resolution': f.get('resolution'),
                        'filesize': f.get('filesize'),
                        'type': 'progressive'
                    })
                # Streams de video adaptativos (solo video) - Filtramos duplicados
                elif f.get('vcodec') != 'none' and f.get('acodec') == 'none':
                    resolution_key = f.get('resolution')
                    if resolution_key not in seen_resolutions:
                        seen_resolutions.add(resolution_key)
                        adaptive_video_options.append({
                            'format_id': f.get('format_id'),
                            'resolution': f.get('resolution'),
                            'filesize': f.get('filesize'),
                            'type': 'adaptive_video'
                        })
                # Streams de solo audio
                elif f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    audio_codec = f.get('acodec')
                    bitrate = f.get('abr') # Tasa de bits promedio
                    format_note = f.get('format_note', '').lower()
                    audio_lang = "Desconocido"
                    if "spanish" in format_note or "español" in format_note:
                        audio_lang = "Español"
                    elif "english" in format_note:
                        audio_lang = "Inglés"
                    elif "audio only" in format_note:
                        audio_lang = "Desconocido"
                    
                    audio_only_options.append({
                        'format_id': f.get('format_id'),
                        'audio_codec': audio_codec,
                        'bitrate': bitrate,
                        'filesize': f.get('filesize'),
                        'type': 'audio_only',
                        'language': audio_lang
                    })

            # Ordenamos las opciones de video por resolución (de mayor a menor)
            adaptive_video_options.sort(key=lambda x: int(x['resolution'].split('x')[1]) if 'x' in x['resolution'] else 0, reverse=True)
            
            logging.info(f"Encontradas {len(progressive_options)} opciones progresivas, {len(adaptive_video_options)} opciones de video adaptativo y {len(audio_only_options)} opciones de solo audio.")

            # Devolvemos las opciones en un objeto JSON
            return jsonify({
                "title": video_title,
                "progressive_options": progressive_options,
                "adaptive_video_options": adaptive_video_options,
                "audio_only_options": audio_only_options
            })

    except yt_dlp.utils.DownloadError as e:
        logging.error(f"yt-dlp DownloadError: {e}")
        return jsonify({"error": f"Error de descarga: {e}"}), 400
    except Exception as e:
        logging.critical(f"Error inesperado en get_download_options: {e}")
        return jsonify({"error": f"Ocurrió un error inesperado: {e}"}), 500

# Endpoint para manejar la descarga
@app.route('/api/download', methods=['POST'])
def download():
    """
    Este endpoint maneja la descarga real de los streams seleccionados.
    """
    logging.info("Llamada a la API: /api/download")
    temp_folder = None
    try:
        data = request.json
        video_url = data.get('url')
        stream_type = data.get('stream_type')
        video_format_id = data.get('video_format_id')
        audio_format_id = data.get('audio_format_id')
        
        logging.info(f"Solicitud de descarga: URL={video_url}, Tipo={stream_type}, ID de Video={video_format_id}, ID de Audio={audio_format_id}")

        if not video_url or not stream_type:
            logging.error("Parámetros de descarga incompletos.")
            return jsonify({"error": "Parámetros de descarga incompletos."}), 400

        # Verificamos que ffmpeg existe en la ruta especificada
        ffmpeg_exists = os.path.exists(FFMPEG_PATH)
        logging.info(f"Verificando FFmpeg en la ruta: {FFMPEG_PATH}. Existe: {ffmpeg_exists}")
        if not ffmpeg_exists:
            logging.error(f"ffmpeg.exe no se encuentra en la ruta especificada: {FFMPEG_PATH}")
            return jsonify({"error": f"Error: No se encontró ffmpeg.exe en la ruta especificada: {FFMPEG_PATH}"}), 500

        temp_folder = os.path.join(DOWNLOAD_FOLDER, str(int(time.time() * 1000)))
        os.makedirs(temp_folder, exist_ok=True)
        logging.info(f"Carpeta temporal creada: {temp_folder}")
        
        # Opciones base para yt-dlp
        ydl_opts = {
            'quiet': True,
            'outtmpl': os.path.join(temp_folder, '%(title)s.%(ext)s'),
            'ffmpeg_location': os.path.dirname(FFMPEG_PATH)
        }

        download_path = None
        final_filename = None

        if stream_type == 'adaptive_video' and video_format_id and audio_format_id:
            logging.info("Manejando la descarga de video adaptativo (combinación de video + audio)")
            
            # Descargar stream de video
            video_ydl_opts = ydl_opts.copy()
            video_ydl_opts['format'] = video_format_id
            video_ydl_opts['outtmpl'] = os.path.join(temp_folder, 'video.%(ext)s')
            with yt_dlp.YoutubeDL(video_ydl_opts) as ydl:
                info_dict = ydl.extract_info(video_url, download=True)
                video_file = ydl.prepare_filename(info_dict)
            
            # SEGUNDO: Verificar y mostrar por consola que se descarga video
            print(f"✅ Video descargado con éxito en: {video_file}")
            
            # Descargar stream de audio
            audio_ydl_opts = ydl_opts.copy()
            audio_ydl_opts['format'] = audio_format_id
            audio_ydl_opts['outtmpl'] = os.path.join(temp_folder, 'audio.%(ext)s')
            with yt_dlp.YoutubeDL(audio_ydl_opts) as ydl:
                info_dict = ydl.extract_info(video_url, download=True)
                audio_file = ydl.prepare_filename(info_dict)
            
            # TERCERO: Verificar y mostrar por consola que se descarga audio
            print(f"✅ Audio descargado con éxito en: {audio_file}")
            
            # Combinar con ffmpeg
            combined_file = os.path.join(temp_folder, f'{info_dict.get("title", "video").replace("/", "_")}.mp4')
            logging.info(f"Iniciando la combinación con FFmpeg. Video: {video_file}, Audio: {audio_file}, Salida: {combined_file}")
            
            command = [FFMPEG_PATH, '-i', video_file, '-i', audio_file, '-c', 'copy', combined_file, '-y']
            logging.info(f"Comando FFmpeg: {' '.join(command)}")
            subprocess.run(command, check=True)
            
            # CUARTO: Verificar y mostrar que el video y audio fueron unidos con éxito
            print("✅ Video y audio unidos exitosamente.")
            
            download_path = combined_file
            final_filename = os.path.basename(download_path)
            logging.info(f"Archivos combinados. Archivo final: {download_path}")

        elif stream_type == 'audio_only' and audio_format_id:
            logging.info("Manejando la descarga de solo audio")
            # Descargar solo el stream de audio
            ydl_opts['format'] = audio_format_id
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'preferredquality': '192'
            }]
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(video_url, download=True)
                download_path = ydl.prepare_filename(info_dict).rsplit('.', 1)[0] + '.m4a'
                final_filename = os.path.basename(download_path)
            logging.info(f"Archivo de solo audio descargado: {download_path}")
            
        elif stream_type == 'progressive' and video_format_id:
            logging.info("Manejando la descarga de video progresivo")

            # Paso 1: Descargar el video sin audio
            video_ydl_opts = ydl_opts.copy()
            video_ydl_opts['format'] = f'{video_format_id}+bestaudio/best'
            video_ydl_opts['outtmpl'] = os.path.join(temp_folder, 'video.mp4')
            video_ydl_opts['postprocessors'] = [{
                'key': 'FFmpegVideoRemuxer',
                'preferedformat': 'mp4'
            }]

            with yt_dlp.YoutubeDL(video_ydl_opts) as ydl:
                info_dict = ydl.extract_info(video_url, download=True)
                video_file = os.path.join(temp_folder, f'{info_dict.get("title", "video").replace("/", "_")}.mp4')
            
            # SEGUNDO: Verificar y mostrar por consola que se descarga video
            print(f"✅ Video descargado con éxito en: {video_file}")
            
            # TERCERO: Extraer el audio del archivo progresivo
            audio_file = os.path.join(temp_folder, 'audio.mp4')
            audio_extract_command = [FFMPEG_PATH, '-i', video_file, '-vn', '-acodec', 'copy', audio_file, '-y']
            subprocess.run(audio_extract_command, check=True)
            
            # TERCERO: Verificar y mostrar por consola que se descarga audio
            print(f"✅ Audio extraído con éxito en: {audio_file}")

            # CUARTO: Unir el video sin audio con el audio extraído
            combined_file = os.path.join(temp_folder, f'{info_dict.get("title", "video").replace("/", "_")}_combined.mp4')
            combine_command = [FFMPEG_PATH, '-i', video_file, '-i', audio_file, '-c', 'copy', combined_file, '-y']
            subprocess.run(combine_command, check=True)
            
            # CUARTO: Verificar y mostrar que el video y audio fueron unidos con éxito
            print("✅ Video y audio unidos exitosamente.")
            
            download_path = combined_file
            final_filename = os.path.basename(download_path)
            logging.info(f"Archivos combinados. Archivo final: {download_path}")

        else:
            logging.error("Parámetros de descarga no válidos.")
            return jsonify({"error": "Parámetros de descarga no válidos."}), 400

        if not download_path or not final_filename:
            logging.error("La ruta de descarga o el nombre de archivo están vacíos. Algo salió mal.")
            return jsonify({"error": "No se pudo preparar la descarga."}), 500

        # Servir el archivo al cliente
        logging.info(f"Sirviendo el archivo: {download_path}")
        return send_file(download_path, as_attachment=True, download_name=final_filename)

    except subprocess.CalledProcessError as e:
        logging.error(f"Error de subproceso de FFmpeg: {e}")
        return jsonify({"error": f"Error al combinar el video con ffmpeg: {e}"}), 500
    except Exception as e:
        logging.critical(f"Error inesperado en el proceso de descarga: {e}")
        return jsonify({"error": f"Ocurrió un error inesperado al descargar el archivo: {e}"}), 500
    finally:
        # Limpiar la carpeta de descargas después de servir el archivo
        if temp_folder and os.path.exists(temp_folder):
            logging.info(f"Limpiando carpeta temporal: {temp_folder}")
            shutil.rmtree(temp_folder, ignore_errors=True)
            
        logging.info("Proceso de descarga finalizado.")
        
# Iniciar el servidor
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)