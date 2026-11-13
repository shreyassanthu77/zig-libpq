"""Regenerates generated/ from the PostgreSQL sources.

Run by `zig build gen`, from the repository root:

    python3 -I gen/gen.py <postgresql-source-dir> <zig-exe> \
        (<name> <zig-triple> <openssl-include-dir> <libssl> <libcrypto>)...

Needs python3, meson, ninja, perl, flex, bison and llvm-nm on top of Zig.
Every platform is configured by PostgreSQL's own meson build with `zig cc` as
the cross compiler and built once; the source lists, flags and generated
headers are then read back from meson and ninja, so nothing here
re-implements PostgreSQL's configuration logic.

libpgcommon and libpgport are archives, so libpq's shared library only gets
the files it needs from them. The manifest lists just those files: the rest
may need symbols libpq doesn't provide, and on Windows exec.c defines
_CRT_glob, which the C runtime would pull into every program that links a
libpq with all of them.
"""

import concurrent.futures
import filecmp
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

OUT = 'generated'
WORK = os.path.join('.zig-cache', 'libpq-gen')
STAGE = os.path.join(WORK, 'stage')

# Everything libpq can use but zig-libpq does not build. libpq only links
# OpenSSL; zlib, zstd and lz4 are used by the server and tools.
MESON_OPTIONS = [
    '-Dssl=openssl',
    '-Duuid=none',
    '-Dllvm=disabled',
    *(f'-D{opt}=disabled' for opt in (
        'bonjour', 'bsd_auth', 'docs', 'docs_pdf', 'gssapi', 'icu', 'ldap',
        'libcurl', 'libnuma', 'liburing', 'libxml', 'libxslt', 'lz4', 'nls',
        'pam', 'plperl', 'plpython', 'pltcl', 'readline', 'selinux', 'systemd',
        'tap_tests', 'zlib', 'zstd',
    )),
]

# The libraries that make up libpq, as meson builds them for libpq's shared
# library: the libpgcommon and libpgport variants for shared libraries, with
# the helper archives meson builds for files that need their own flags.
# libpq's own files are taken from the static library, which differs from
# the shared one only in not loading the OAuth flow from a separate module.
# (The static libpgcommon renames the encoding functions libpq exports.)
# The values are the manifest group names.
LIBRARIES = {
    'libpq': 'libpq',
    'libpgcommon_shlib': 'common',
    'libpgport_shlib': 'port',
}
HELPER = re.compile(r'^(libpgcommon_shlib|libpgport_shlib)_\w+$')


# Installed public headers, with their install paths: libpq's API, the
# large-object constants and the one header libpq-fe.h includes.
HEADERS = [
    ('src/interfaces/libpq/libpq-fe.h', 'libpq-fe.h'),
    ('src/interfaces/libpq/libpq-events.h', 'libpq-events.h'),
    ('src/include/libpq/libpq-fs.h', 'libpq/libpq-fs.h'),
    ('src/include/postgres_ext.h', 'postgres_ext.h'),
]

# Flags kept from meson's compile lines; warnings, optimization, debug info,
# dependency files and include paths are the consumer's business or are
# recorded separately.
KEEP_FLAG = re.compile(r'^(-D|-U|-std=|-m|-pthread$|-f(?!diagnostics-color|PIC$|pic$))')

ARCH = {
    'x86_64': ('x86_64', 'little'),
    'aarch64': ('aarch64', 'little'),
    'riscv64': ('riscv64', 'little'),
    'powerpc64le': ('ppc64', 'little'),
    's390x': ('s390x', 'big'),
}
SYSTEM = {'linux': 'linux', 'macos': 'darwin', 'windows': 'windows'}



def main(argv):
    src = os.path.realpath(argv[1])
    zig = argv[2]
    rest = argv[3:]
    if not rest or len(rest) % 5:
        die('usage: gen.py <postgresql-source> <zig> (<name> <triple> <include> <libssl> <libcrypto>)...')
    platforms = [Platform(*rest[i:i + 5]) for i in range(0, len(rest), 5)]

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(STAGE)
    fake_windres()

    print(f'gen: configuring and building {len(platforms)} platforms in parallel', flush=True)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        list(pool.map(lambda p: p.build(src, zig), platforms))

    results = []
    for p in platforms:
        print(f'gen: extracting {p.name}', flush=True)
        results.append(p.extract(src))

    write_manifest(src, platforms, results)
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.move(STAGE, OUT)
    print(f'gen: wrote {OUT}')


class Platform:
    def __init__(self, name, triple, include, libssl, libcrypto):
        self.name = name
        self.triple = triple
        self.openssl_include = os.path.realpath(include)
        self.libssl = os.path.realpath(libssl)
        self.libcrypto = os.path.realpath(libcrypto)
        parts = triple.split('-')
        if len(parts) != 3:
            die(f'{name}: target {triple} must be <arch>-<os>-<abi>')
        self.arch, self.os, self.abi = parts
        if self.os == 'macos':
            self.abi = 'none'
        if self.arch not in ARCH or self.os not in SYSTEM:
            die(f'{name}: unsupported target {triple}')
        self.build_dir = os.path.realpath(os.path.join(WORK, name))

    def build(self, src, zig):
        """Configures PostgreSQL for this platform and builds libpq."""
        os.makedirs(self.build_dir)
        log = open(os.path.join(WORK, f'{self.name}.log'), 'w')
        cross = self.write_cross_file(zig)
        run(['meson', 'setup', self.build_dir, '--cross-file', cross, *MESON_OPTIONS],
            cwd=src, log=log, what=self.name)
        self.targets = self.libpq_targets()
        outputs = [os.path.relpath(f, self.build_dir) for t in self.targets for f in t['filename']]
        run(['ninja', '-C', self.build_dir, *outputs], log=log, what=self.name)

    def write_cross_file(self, zig):
        pc = os.path.join(WORK, f'{self.name}-pkgconfig')
        os.makedirs(pc)
        libs = f'{self.libssl} {self.libcrypto}'
        if self.os == 'windows':
            libs += ' -lws2_32 -lcrypt32 -luserenv -ladvapi32 -lgdi32 -lbcrypt'
        else:
            libs += ' -pthread'
        with open(os.path.join(pc, 'openssl.pc'), 'w') as f:
            f.write('Name: openssl\nDescription: zig-openssl\nVersion: 4.1.0\n'
                    f'Cflags: -I{self.openssl_include}\nLibs: {libs}\n')

        family, endian = ARCH[self.arch]
        cc = [zig, 'cc', '-target', self.triple]
        path = os.path.realpath(os.path.join(WORK, f'{self.name}.ini'))
        with open(path, 'w') as f:
            f.write('[binaries]\n')
            f.write(f'c = {cc!r}\n')
            f.write(f'ar = {[zig, "ar"]!r}\n')
            f.write(f'strip = {[zig, "objcopy", "--strip-all"]!r}\n')
            f.write("pkg-config = 'pkg-config'\n")
            # Required at configure time for the DLLs' version resources,
            # which zig-libpq does not build.
            f.write(f'windres = {fake_windres()!r}\n')
            f.write('[built-in options]\n')
            f.write(f'c_args = {extra_cflags(self)!r}\n')
            f.write('[properties]\n')
            f.write(f'pkg_config_libdir = {os.path.realpath(pc)!r}\n')
            f.write('[host_machine]\n')
            f.write(f"system = '{SYSTEM[self.os]}'\n")
            f.write(f"cpu_family = '{family}'\ncpu = '{self.arch}'\nendian = '{endian}'\n")
        return path

    def libpq_targets(self):
        info = os.path.join(self.build_dir, 'meson-info', 'intro-targets.json')
        with open(info) as f:
            targets = json.load(f)
        chosen = [t for t in targets
                  if t['type'] == 'static library'
                  and (t['name'] in LIBRARIES or HELPER.match(t['name']))]
        names = {t['name'] for t in chosen}
        missing = set(LIBRARIES) - names
        if missing:
            die(f'{self.name}: meson has no {", ".join(sorted(missing))} target')
        return chosen

    def extract(self, src):
        """Reads back the units, flags, includes and headers of this platform."""
        units = []  # (group, path, flags)
        includes = {}  # group -> source-relative include dirs, in order
        build_includes = set()
        for t in self.targets:
            group = LIBRARIES[HELPER.sub(r'\1', t['name'])]
            for s in t['target_sources']:
                if s.get('language') != 'c':
                    continue
                flags, incs = self.split_parameters(src, s['parameters'])
                if includes.setdefault(group, incs['source']) != incs['source']:
                    die(f'{self.name}: {group} include directories differ between files')
                build_includes.update(incs['build'])
                for path in s['sources']:
                    path = os.path.relpath(path, src)
                    if path.startswith('..'):
                        die(f'{self.name}: generated C source {path} is not supported')
                    units.append((group, path, tuple(flags)))

        # Group flags: the ones every file of the group shares. Whatever is
        # left is specific to the file.
        needed = self.needed_sources(src)
        units = [u for u in units if u[0] == 'libpq' or u[1] in needed]

        group_flags = {}
        for group in includes:
            lists = [f for g, _, f in units if g == group]
            group_flags[group] = [f for f in lists[0] if all(f in l for l in lists)]
        units = [(g, p, tuple(f for f in flags if f not in group_flags[g]))
                 for g, p, flags in units]

        headers = self.generated_headers(sorted(build_includes))
        stage = os.path.join(STAGE, '.platforms', self.name)
        for name, path in headers.items():
            dst = os.path.join(stage, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(path, dst)
            for group, dirs in includes.items():
                for d in dirs:
                    if os.path.exists(os.path.join(src, d, name)):
                        die(f'{self.name}: generated {name} is shadowed by {d}/{name}')

        return {
            'units': sorted(set(units)),
            'includes': includes,
            'group_flags': group_flags,
            'headers': sorted(headers),
            'links': self.link_flags(),
        }

    def needed_sources(self, src):
        """The sources whose objects linking libpq's own pulls from the archives."""
        objects = {}  # object path -> (group, source path)
        for t in self.targets:
            group = LIBRARIES[HELPER.sub(r'\1', t['name'])]
            for f in t['filename']:
                rel = os.path.relpath(f, self.build_dir)
                commands = subprocess.run(['ninja', '-C', self.build_dir, '-t', 'commands', rel],
                                          check=True, capture_output=True, text=True).stdout
                for line in commands.splitlines():
                    args = shlex.split(line)
                    if '-c' in args and '-o' in args:
                        source = os.path.realpath(os.path.join(self.build_dir, args[args.index('-c') + 1]))
                        obj = os.path.join(self.build_dir, args[args.index('-o') + 1])
                        objects[obj] = (group, os.path.relpath(source, src))

        defined = {}  # symbol -> object
        undefined = {}  # object -> symbols
        for obj in objects:
            undefined[obj] = []
            nm = subprocess.run(['llvm-nm', '-g', '-P', obj], check=True,
                                capture_output=True, text=True).stdout
            for line in nm.splitlines():
                fields = line.split()
                if len(fields) < 2:
                    continue
                if fields[1] in 'Uw':
                    undefined[obj].append(fields[0])
                elif objects[obj][0] != 'libpq':
                    defined.setdefault(fields[0], obj)

        # Roots: libpq's own objects and its exported API, some of which
        # libpgcommon implements.
        with open(os.path.join(src, 'src/interfaces/libpq/exports.txt')) as f:
            exports = [line.split()[0] for line in f if line.strip() and not line.startswith('#')]
        prefix = '_' if self.os == 'macos' else ''
        undefined[None] = [prefix + sym for sym in exports]

        pulled = set()
        work = [None] + [o for o, (g, _) in objects.items() if g == 'libpq']
        while work:
            for sym in undefined[work.pop()]:
                obj = defined.get(sym)
                if obj and obj not in pulled:
                    pulled.add(obj)
                    work.append(obj)
        return {objects[o][1] for o in pulled}

    def split_parameters(self, src, params):
        flags = []
        incs = {'source': [], 'build': []}
        for p in params:
            if p.startswith('-I'):
                d = os.path.realpath(os.path.join(self.build_dir, p[2:]))
                if d == self.openssl_include:
                    continue
                if is_under(d, self.build_dir):
                    rel = os.path.relpath(d, self.build_dir)
                    if not rel.endswith('.p'):  # meson's per-target private dir
                        incs['build'].append(rel)
                elif is_under(d, src):
                    incs['source'].append(os.path.relpath(d, src))
                else:
                    die(f'{self.name}: include directory outside the build: {d}')
            elif p.startswith('-DVAL_'):
                # config_info.c records the build's compiler and flags for
                # pg_config; they would name this machine's paths.
                flags.append(p.split('=', 1)[0] + '=""')
            elif KEEP_FLAG.match(p) or p in extra_cflags(self):
                if ' ' in p:
                    die(f'{self.name}: flag {p!r} contains a space')
                flags.append(p)
        return flags, incs

    def generated_headers(self, build_includes):
        """Maps the include name of every generated header the build used to its path."""
        deps = subprocess.run(['ninja', '-C', self.build_dir, '-t', 'deps'],
                              check=True, capture_output=True, text=True).stdout
        headers = {}
        for line in deps.splitlines():
            if not line.startswith(' '):
                continue
            path = os.path.realpath(os.path.join(self.build_dir, line.strip()))
            if not is_under(path, self.build_dir):
                continue
            rel = os.path.relpath(path, self.build_dir)
            dirs = [d for d in build_includes if rel.startswith(d + os.sep)]
            if not dirs:
                die(f'{self.name}: {rel} is not under any include directory')
            name = os.path.relpath(rel, max(dirs, key=len))
            headers[name] = path
        for name, path in headers.items():
            with open(path, errors='replace') as f:
                text = f.read()
            if WORK in text or os.path.realpath(WORK) in text:
                die(f'{self.name}: {name} mentions the build directory')
        return headers

    def link_flags(self):
        """libpq's Libs.private, without the libraries zig-libpq merges in."""
        with open(os.path.join(self.build_dir, 'meson-private', 'libpq.pc')) as f:
            text = f.read()
        m = re.search(r'^Libs\.private:(.*)$', text, re.M)
        flags = m.group(1).split() if m else []
        return [f for f in flags if not f.startswith('-L') and f not in ('-lpgport', '-lpgcommon')]


def extra_cflags(platform):
    """Flags of our own, recorded in the manifest like meson's."""
    if platform.os == 'windows':
        # On MinGW-w64, c.h maps sigsetjmp to __builtin_setjmp, whose
        # parameter clang declares as void **; GCC accepts the sigjmp_buf
        # it is given.
        return ['-Wno-incompatible-pointer-types']
    if platform.arch == 'powerpc64le' and platform.abi == 'gnu':
        # Match zig-openssl: the IBM long double ABI, which glibc < 2.32
        # supports.
        return ['-mabi=ibmlongdouble']
    return []


def write_manifest(src, platforms, results):
    """Hoists everything every platform agrees on, then writes the manifest."""
    # Generated headers identical everywhere go to common/.
    names = set.intersection(*(set(r['headers']) for r in results))
    shared = set()
    for name in sorted(names):
        files = [os.path.join(STAGE, '.platforms', p.name, name) for p in platforms]
        if all(filecmp.cmp(files[0], f, shallow=False) for f in files[1:]):
            shared.add(name)
            dst = os.path.join(STAGE, 'common', name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(files[0], dst)
    for p, r in zip(platforms, results):
        for name in r['headers']:
            if name not in shared:
                dst = os.path.join(STAGE, p.name, name)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(STAGE, '.platforms', p.name, name), dst)
    shutil.rmtree(os.path.join(STAGE, '.platforms'))

    groups = list(dict.fromkeys(g for r in results for g in r['includes']))
    # Include order matters, so only a prefix every platform shares is
    # hoisted; platforms add the rest after it.
    common_includes = {}
    for g in groups:
        lists = [r['includes'].get(g, []) for r in results]
        n = 0
        while all(len(l) > n and l[n] == lists[0][n] for l in lists):
            n += 1
        common_includes[g] = lists[0][:n]

    def everywhere(key):
        return [x for x in key(results[0]) if all(x in key(r) for r in results[1:])]

    common_units = set(everywhere(lambda r: r['units']))
    common_flags = {g: everywhere(lambda r: r['group_flags'].get(g, [])) for g in groups}

    with open('build.zig.zon') as f:
        zon = f.read()
    url = re.search(r'\.postgresql = \.\{\s*\.url = "([^"]+)",\s*\.hash = "([^"]+)"', zon)
    with open(os.path.join(src, 'meson.build')) as f:
        version = re.search(r"version: '([^']+)'", f.read()).group(1)

    lines = []
    with open(os.path.join('gen', 'manifest-header.txt')) as f:
        lines.append(f.read().rstrip('\n'))
    lines += ['version 1', f'postgresql {version}', f'upstream {url.group(1)} {url.group(2)}',
              f'shared-dir {OUT}/common', '']
    for path, name in HEADERS:
        if not os.path.exists(os.path.join(src, path)):
            die(f'no header {path}')
        lines.append(f'header {path} {name}')
    lines.append('')
    for g in groups:
        lines += [f'cflag {g} {f}' for f in common_flags[g]]
        lines += [f'include {g} {d}' for d in common_includes[g]]
    lines.append('')
    lines += [unit_record(u) for u in sorted(common_units)]
    lines.append('')

    for p, r in zip(platforms, results):
        lines.append(f'platform {p.arch} {p.os} {p.abi} {OUT}/{p.name}')
        lines.append(f'triple {p.triple}')
        lines += [f'link {f}' for f in r['links']]
        for g in groups:
            lines += [f'cflag {g} {f}' for f in r['group_flags'].get(g, []) if f not in common_flags[g]]
            lines += [f'include {g} {d}' for d in r['includes'].get(g, [])[len(common_includes[g]):]]
        lines += [unit_record(u) for u in r['units'] if u not in common_units]
        lines.append('')

    with open(os.path.join(STAGE, 'manifest.txt'), 'w') as f:
        f.write('\n'.join(lines))


def fake_windres():
    """A windres that meson can identify and that fails if ever run."""
    path = os.path.realpath(os.path.join(WORK, 'windres'))
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n'
                    'if [ "$1" = --version ]; then echo "GNU windres (GNU Binutils) 2.42"; exit 0; fi\n'
                    'echo "zig-libpq: resources are not built" >&2; exit 1\n')
        os.chmod(path, 0o755)
    return path


def unit_record(unit):
    group, path, flags = unit
    return ' '.join(['source', group, path, *flags])


def is_under(path, root):
    return path == root or path.startswith(root + os.sep)


def run(cmd, cwd=None, log=None, what=''):
    r = subprocess.run(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        if log:
            log.flush()
            with open(log.name) as f:
                sys.stderr.write(''.join(f.readlines()[-40:]))
        die(f'{what}: {" ".join(cmd[:2])} failed, see {log.name if log else "output"}')


def die(message):
    sys.stderr.write(f'gen: {message}\n')
    sys.exit(1)


if __name__ == '__main__':
    main(sys.argv)
