# Runs on Hugging Face Spaces (Docker SDK, port 7860) and anywhere else that runs Docker.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 user
USER user
ENV PATH=/home/user/.local/bin:$PATH \
    CUTWARDEN_WORKDIR=/home/user/work \
    CUTWARDEN_BRAND=/home/user/app/brands/example/brand.json
WORKDIR /home/user/app
COPY --chown=user pyproject.toml README.md LICENSE ./
COPY --chown=user cutwarden ./cutwarden
COPY --chown=user brands ./brands
RUN pip install --no-cache-dir .
EXPOSE 7860
CMD ["cutwarden-mcp", "--http", "--host", "0.0.0.0", "--port", "7860"]
