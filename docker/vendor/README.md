# Vendored build sources

These two source archives are inputs to `docker/Dockerfile.postgres`. They live in
the repository on purpose.

## Why vendored instead of downloaded during the build

`github.com` is not reachable from the build container in this environment, so a
`git clone` / `curl` in the Dockerfile fails. Fetching from third-party mirrors
would make the image depend on whichever mirror happens to be up. Pinning the
exact archives here makes the build reproducible and lets it work offline.

Both files are checksum-verified by hand; they are not fetched by the build.

## Contents

| File | Upstream | Version | sha256 |
|---|---|---|---|
| `scws.tar.bz2` | <https://www.xunsearch.com/scws/> (release tarball) | 1.2.3 | `60d50ac3dc42cff3c0b16cb1cfee47d8cb8c8baa142a58bc62854477b81f1af5` |
| `zhparser.tar.gz` | <https://github.com/amutu/zhparser> tag `v2.3` | 2.3 (`dd292fd`) | `e177ef3815eadbc56e4b7f69c8ede3b07dc6326d172047584648ac0ac32e55ce` |

SCWS is the Chinese segmenter that zhparser links against. Neither library is
packaged for Debian, so both are compiled in the image (spec §5).

`zhparser.tar.gz` is ~6 MB compressed because it bundles `dict.utf8.xdb`
(14 MB uncompressed), the segmentation dictionary that `make install` places in
`share/tsearch_data`. It is required at runtime, not just at build time.

## Re-vendoring

```bash
# SCWS — release tarball, already contains a working `configure`
curl -fsSL -o scws.tar.bz2 \
  https://www.xunsearch.com/scws/down/scws-1.2.3.tar.bz2

# zhparser — pinned to a tag; git protocol is used because HTTPS to github.com
# is blocked from this environment
git clone --depth 1 --branch v2.3 git@github.com:amutu/zhparser.git zhparser-src
rm -rf zhparser-src/.git
tar -czf zhparser.tar.gz zhparser-src && rm -rf zhparser-src

sha256sum scws.tar.bz2 zhparser.tar.gz   # update the table above
```

When bumping either version, update `PG_MAJOR` / the checksums here and rebuild:
the `chinese` text search configuration is declared once in `docker/initdb/`, and
`zhparser`'s SQL migration files are versioned in its `.control` file.

## Licences

- SCWS: BSD-style (see the `COPYRIGHT` file inside the archive)
- zhparser: PostgreSQL licence (see `COPYRIGHT` inside the archive)
