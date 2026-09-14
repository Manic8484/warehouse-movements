FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application and Flask templates into the container.
COPY app.py .
COPY templates ./templates

CMD exec gunicorn --bind :$PORT --workers 2 --threads 4 --timeout 0 app:app
