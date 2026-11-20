FROM python:3.13-slim

# Run as a non-root user
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .

ENV HOST=0.0.0.0 PORT=7860 RELOAD=0 EMBEDDED=1

EXPOSE 7860
CMD ["python", "app.py"]
