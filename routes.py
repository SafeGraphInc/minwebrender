import markdown
from flask import render_template, request, redirect, jsonify, Response, send_file
import io
import json
import asyncio


def init_routes(app, browser_service, async_loop):
    @app.route('/', methods=['GET', 'POST'])
    def home():
        if request.method == 'POST':
            user_url = request.form['url']
            return redirect(f"/{user_url}")

        return render_template('home.html')

    @app.route('/<path:url>')
    def fetch_minimal_page(url):
        try:
            if not url.startswith('http'):
                full_url = f"http://{url}"
            else:
                full_url = url

            future = asyncio.run_coroutine_threadsafe(browser_service.render_page_and_extract_text(full_url),
                                                      async_loop)
            markdown_text = future.result()

            html_content = markdown.markdown(markdown_text)

            return render_template('rendered_page.html', content=html_content, markdown_content=markdown_text,
                                   url=full_url)

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route('/download', methods=['POST'])
    def download_content():
        content = request.form.get('content')
        url = request.form.get('url')
        format_type = request.form.get('format')

        if not content:
            return "No content to download", 400

        if format_type == 'text':
            # Create a file-like object for text
            buffer = io.BytesIO()
            buffer.write(content.encode('utf-8'))
            buffer.seek(0)
            return send_file(
                buffer,
                as_attachment=True,
                download_name='download.txt',
                mimetype='text/plain'
            )

        elif format_type == 'markdown':
            # Create a file-like object for markdown
            buffer = io.BytesIO()
            buffer.write(content.encode('utf-8'))
            buffer.seek(0)
            return send_file(
                buffer,
                as_attachment=True,
                download_name='download.md',
                mimetype='text/markdown'
            )

        elif format_type == 'jsonl':
            data = {
                "url": url,
                "content": content
            }
            json_line = json.dumps(data, ensure_ascii=False)

            buffer = io.BytesIO()
            buffer.write(json_line.encode('utf-8'))
            buffer.write(b'\n')  # Good practice for JSONL
            buffer.seek(0)

            return send_file(
                buffer,
                as_attachment=True,
                download_name='download.jsonl',
                mimetype='application/x-jsonlines'
            )

        return "Invalid format", 400
