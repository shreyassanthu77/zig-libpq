# zig-libpq

libpq, the PostgreSQL 18.7 client library, built with Zig and linked against
[zig-openssl](https://github.com/shreyassanthu77/zig-openssl). Everything
PostgreSQL normally generates at build time (`pg_config.h`, the Perl-generated
headers, the per-platform source lists) is checked into `generated/`, so a
normal build needs only Zig.

```sh
zig build                                  # static libpq for the native target
zig build -Dtarget=aarch64-linux-musl      # cross
zig build examples                         # also libpq's example programs
```

Targets: x86_64/aarch64/riscv64/powerpc64le/s390x Linux (gnu and musl),
x86_64/aarch64 macOS, x86_64 Windows (gnu).

The library is libpq with SSL (OpenSSL 4.1.0) and SCRAM; no GSSAPI, LDAP,
OAuth device flow or NLS. It holds libpq and the parts of PostgreSQL's
libpgcommon and libpgport it uses, as in libpq's shared library. Installed
headers: `libpq-fe.h`, `libpq-events.h`, `libpq/libpq-fs.h`,
`postgres_ext.h`.

Link consumers with OpenSSL (`libssl`, `libcrypto`), plus `-lm` on Unix and
`-lws2_32 -lsecur32` on Windows; see the zig-openssl README for OpenSSL's own
system libraries. On powerpc64le glibc, compile consumers with
`-mabi=ibmlongdouble`, like libpq and OpenSSL.

## Other build systems

`generated/manifest.txt` lists the sources, flags, include directories and
link flags for every platform. Its header documents the format.

## Regenerating

```sh
zig build gen
```

This runs `gen/gen.py`, which needs `python3`, `meson`, `ninja`, `perl`,
`flex`, `bison` and `llvm-nm`. It builds zig-openssl for every platform,
configures PostgreSQL with its own meson build and `zig cc` as the cross
compiler, builds libpq, reads the sources, flags and generated headers back
from meson and ninja, and replaces `generated/`.

## License

PostgreSQL is under the PostgreSQL License; the generated files are derived
from it and carry the same terms. See `COPYRIGHT`.
