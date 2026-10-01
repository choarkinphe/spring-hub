# syntax=docker/dockerfile:1.7
FROM rust:1.82-bookworm AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock* ./
RUN cargo fetch
COPY src ./src
COPY migrations ./migrations
COPY web ./web
RUN cargo build --release

FROM debian:bookworm-slim
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini wget \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cute-cat \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cute-cat
WORKDIR /app
COPY --from=builder /src/target/release/cute-cat /app/cute-cat
COPY config.example.toml /app/config.example.toml
RUN mkdir -p /data /media && chown -R cute-cat:cute-cat /app /data /media
ENV CUTE_CAT_CONFIG=/app/config.toml
EXPOSE 8080
USER cute-cat
ENTRYPOINT ["/usr/bin/tini", "--", "/app/cute-cat"]
