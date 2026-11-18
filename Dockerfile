FROM python:3.13-slim

# Hugging Face Spaces run the container as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .

# app.py reads the synthetic data in data/ by default. Proposals are written
# inside the container and are lost whenever the Space restarts or rebuilds.
# ACCESS_CODE and STORAGE_SECRET come from the Space's secrets.
ENV HOST=0.0.0.0 PORT=7860 RELOAD=0 EMBEDDED=1

EXPOSE 7860
CMD ["python", "app.py"]
