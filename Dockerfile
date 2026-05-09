FROM python:3.14-alpine AS builder

WORKDIR /build

COPY docker/nwn-musl-compat.c ./docker/nwn-musl-compat.c

# nwn 0.0.22 ships a glibc-linked NWScript compiler. Alpine's musl does not
# provide a couple of locale parsing symbols it expects, so we preload a tiny
# shim that delegates those calls to musl's locale-independent parsers.
RUN apk add --no-cache gcc musl-dev \
    && mkdir -p /install/lib \
    && gcc -shared -fPIC -O2 \
        -o /install/lib/libnwn-musl-compat.so \
        ./docker/nwn-musl-compat.c

# Install from the source tree in a throwaway stage so the runtime image only
# carries the package and dependencies needed to run the console entry point.
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.14-alpine

RUN apk add --no-cache gcompat

WORKDIR /usr/app

# Copy the installed prefix instead of the full checkout; tests, scripts, and
# build context files are excluded from the final image.
COPY --from=builder /install /usr/local

ENV LD_PRELOAD=/usr/local/lib/libnwn-musl-compat.so

ENTRYPOINT ["arebuilder"]
