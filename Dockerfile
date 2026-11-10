# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /dist

FROM python:3.13-slim
LABEL org.opencontainers.image.title="seonaut-mcp" \
      org.opencontainers.image.description="Unofficial MCP server for SEOnaut crawl results (read-only)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/Abdullah-Abuzaid/seonaut-mcp"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl \
 && useradd --system --uid 10001 --no-create-home seonaut-mcp
USER 10001
EXPOSE 9100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["seonaut-mcp"]
CMD ["serve"]
