const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const upstream = b.dependency("postgresql", .{});

    addGenStep(b, upstream);

    // Read at build-script runtime so `zig build gen` never invalidates the
    // build script itself.
    const manifest_path = b.root.joinString(b.allocator, "generated/manifest.txt") catch @panic("OOM");
    const text = std.Io.Dir.cwd().readFileAlloc(b.graph.io, manifest_path, b.allocator, .unlimited) catch |err| {
        b.graph.poisonCache();
        fail(b, b.fmt("cannot read generated/manifest.txt ({t}); run `zig build gen`", .{err}));
        return;
    };
    b.dependOnFileContents(b.path("generated/manifest.txt"));

    const m = Manifest.parse(b, text, target.result) catch |err| {
        fail(b, b.fmt("generated/manifest.txt: {t}", .{err}));
        return;
    };
    if (m.generated_dir.len == 0) {
        fail(b, b.fmt("no generated sources for {t}-{t}-{t}", .{
            target.result.cpu.arch, target.result.os.tag, target.result.abi,
        }));
        return;
    }

    const openssl = b.dependency("openssl", .{ .target = target, .optimize = optimize });
    const mod = b.createModule(.{
        .target = target,
        .optimize = optimize,
        .link_libc = true,
        .pic = if (target.result.os.tag == .windows) null else true,
        // A static library must not drag the UBSan runtime into the
        // consumer's link.
        .sanitize_c = .off,
    });
    for (m.links.items) |flag| {
        if (std.mem.startsWith(u8, flag, "-l")) mod.linkSystemLibrary(flag[2..], .{});
        // -pthread is part of libc for every target Zig supports.
    }
    mod.linkLibrary(openssl.artifact("ssl"));
    mod.linkLibrary(openssl.artifact("crypto"));
    if (target.result.os.tag == .windows) {
        // What zig-openssl's Windows archives need.
        for ([_][]const u8{ "ws2_32", "crypt32", "userenv", "advapi32", "gdi32", "bcrypt" }) |name| {
            mod.linkSystemLibrary(name, .{});
        }
    }

    // Include directories differ between groups, so they go in each group's
    // flags rather than on the module.
    const src = upstream.builder.root;
    for (m.groups.items) |*group| {
        var flags: std.ArrayList([]const u8) = .empty;
        flags.append(b.allocator, b.fmt("-I{s}", .{b.root.joinString(b.allocator, m.generated_dir) catch @panic("OOM")})) catch @panic("OOM");
        flags.append(b.allocator, b.fmt("-I{s}", .{b.root.joinString(b.allocator, m.shared_dir) catch @panic("OOM")})) catch @panic("OOM");
        for (group.includes.items) |dir| {
            flags.append(b.allocator, b.fmt("-I{s}", .{src.joinString(b.allocator, dir) catch @panic("OOM")})) catch @panic("OOM");
        }
        flags.appendSlice(b.allocator, group.flags.items) catch @panic("OOM");
        for (group.sources.items) |source| {
            const all = std.mem.concat(b.allocator, []const u8, &.{ flags.items, source.flags }) catch @panic("OOM");
            mod.addCSourceFile(.{ .file = upstream.path(source.path), .flags = all });
        }
    }

    const lib = b.addLibrary(.{ .name = "pq", .linkage = .static, .root_module = mod });
    for (m.headers.items) |header| lib.installHeader(upstream.path(header[0]), header[1]);
    b.installArtifact(lib);

    // libpq's example programs, to check that the library links.
    const examples = b.step("examples", "Build libpq's example programs");
    // Consumers must use libpq's long double ABI; see the ppc64le glibc
    // platform's cflag records.
    const example_flags: []const []const u8 = if (target.result.cpu.arch == .powerpc64le and target.result.abi == .gnu)
        &.{"-mabi=ibmlongdouble"}
    else
        &.{};
    for ([_][]const u8{ "testlibpq", "testlibpq2", "testlibpq3", "testlibpq4", "testlo", "testlo64" }) |name| {
        // These two use POSIX socket headers.
        const posix_only = eql(name, "testlibpq2") or eql(name, "testlibpq3");
        if (posix_only and target.result.os.tag == .windows) continue;
        const exe = b.addExecutable(.{
            .name = name,
            .root_module = b.createModule(.{ .target = target, .optimize = optimize, .link_libc = true }),
        });
        exe.root_module.addCSourceFile(.{
            .file = upstream.path(b.fmt("src/test/examples/{s}.c", .{name})),
            .flags = example_flags,
        });
        exe.root_module.linkLibrary(lib);
        examples.dependOn(&b.addInstallArtifact(exe, .{}).step);
    }
}

/// The platforms `zig build gen` generates, by generated directory name and
/// Zig target.
const platforms = [_][2][]const u8{
    .{ "linux-x86_64-gnu", "x86_64-linux-gnu" },
    .{ "linux-x86_64-musl", "x86_64-linux-musl" },
    .{ "linux-aarch64-gnu", "aarch64-linux-gnu" },
    .{ "linux-aarch64-musl", "aarch64-linux-musl" },
    .{ "linux-riscv64-gnu", "riscv64-linux-gnu" },
    .{ "linux-riscv64-musl", "riscv64-linux-musl" },
    .{ "linux-ppc64le-gnu", "powerpc64le-linux-gnu" },
    .{ "linux-ppc64le-musl", "powerpc64le-linux-musl" },
    .{ "linux-s390x-gnu", "s390x-linux-gnu" },
    .{ "linux-s390x-musl", "s390x-linux-musl" },
    .{ "macos-x86_64", "x86_64-macos-none" },
    .{ "macos-aarch64", "aarch64-macos-none" },
    .{ "windows-x86_64", "x86_64-windows-gnu" },
};

/// Maintainer step: needs python3, meson, ninja, perl, flex and bison on top
/// of Zig. PostgreSQL's configuration probes link against OpenSSL, so each
/// platform gets zig-openssl built for it.
fn addGenStep(b: *std.Build, upstream: *std.Build.Dependency) void {
    const gen = b.addSystemCommand(&.{ "python3", "-I" });
    gen.addFileArg(b.path("gen/gen.py"));
    gen.addDirectoryArg(upstream.path(""));
    gen.addArg(b.graph.zig_exe);
    for (platforms) |p| {
        const query = std.Target.Query.parse(.{ .arch_os_abi = p[1] }) catch unreachable;
        const openssl = b.dependency("openssl", .{
            .target = b.resolveTargetQuery(query),
            .optimize = .ReleaseFast,
        });
        const crypto = openssl.artifact("crypto");
        gen.addArgs(&p);
        gen.addDirectoryArg(crypto.getEmittedIncludeTree());
        gen.addArtifactArg(openssl.artifact("ssl"));
        gen.addArtifactArg(crypto);
    }
    gen.setCwd(b.path("."));
    gen.has_side_effects = true;
    b.step("gen", "Regenerate generated/ from the PostgreSQL sources").dependOn(&gen.step);
}

/// The parts of manifest.txt that apply to one target. See the header of
/// generated/manifest.txt for the format.
const Manifest = struct {
    shared_dir: []const u8 = "",
    generated_dir: []const u8 = "",
    /// Public headers: path in the PostgreSQL source tree, install path.
    headers: std.ArrayList([2][]const u8) = .empty,
    groups: std.ArrayList(Group) = .empty,
    /// `link` flags of the target's platform.
    links: List = .empty,

    const List = std.ArrayList([]const u8);

    const Group = struct {
        name: []const u8,
        flags: List = .empty,
        /// Relative to the PostgreSQL source tree.
        includes: List = .empty,
        sources: std.ArrayList(Source) = .empty,
    };

    const Source = struct {
        /// Relative to the PostgreSQL source tree.
        path: []const u8,
        flags: []const []const u8,
    };

    fn parse(b: *std.Build, text: []const u8, target: std.Target) !Manifest {
        const gpa = b.allocator;
        var m: Manifest = .{};
        // Whether we are before the first platform block, or inside the
        // block for this target.
        var selected = true;

        var lines = std.mem.splitScalar(u8, text, '\n');
        while (lines.next()) |line| {
            var fields = std.mem.tokenizeScalar(u8, line, ' ');
            const record = fields.next() orelse continue;
            if (record[0] == '#') continue;
            const arg = fields.next() orelse "";

            if (eql(record, "platform")) {
                const os = fields.next() orelse return error.BadPlatform;
                const abis = fields.next() orelse return error.BadPlatform;
                const dir = fields.next() orelse return error.BadPlatform;
                selected = eql(arg, @tagName(target.cpu.arch)) and
                    eql(os, @tagName(target.os.tag)) and
                    hasAbi(abis, @tagName(target.abi));
                if (selected) m.generated_dir = dir;
            } else if (eql(record, "version") or eql(record, "postgresql") or
                eql(record, "upstream") or eql(record, "triple"))
            {
                // Informational.
            } else if (eql(record, "shared-dir")) {
                m.shared_dir = arg;
            } else if (eql(record, "header")) {
                try m.headers.append(gpa, .{ arg, fields.next() orelse return error.BadHeader });
            } else if (!selected) {
                // A record of some other platform.
            } else if (eql(record, "link")) {
                try m.links.append(gpa, arg);
            } else if (eql(record, "cflag")) {
                const g = try m.getGroup(gpa, arg);
                try g.flags.append(gpa, fields.next() orelse return error.BadCflag);
            } else if (eql(record, "include")) {
                const g = try m.getGroup(gpa, arg);
                try g.includes.append(gpa, fields.next() orelse return error.BadInclude);
            } else if (eql(record, "source")) {
                const g = try m.getGroup(gpa, arg);
                const path = fields.next() orelse return error.BadSource;
                var flags: List = .empty;
                while (fields.next()) |flag| try flags.append(gpa, flag);
                try g.sources.append(gpa, .{ .path = path, .flags = flags.items });
            } else {
                return error.UnknownRecord;
            }
        }
        return m;
    }

    fn getGroup(m: *Manifest, gpa: std.mem.Allocator, name: []const u8) !*Group {
        for (m.groups.items) |*g| if (eql(g.name, name)) return g;
        try m.groups.append(gpa, .{ .name = name });
        return &m.groups.items[m.groups.items.len - 1];
    }
};

fn hasAbi(list: []const u8, abi: []const u8) bool {
    var it = std.mem.splitScalar(u8, list, ',');
    while (it.next()) |candidate| if (eql(candidate, abi)) return true;
    return false;
}

fn eql(a: []const u8, b: []const u8) bool {
    return std.mem.eql(u8, a, b);
}

fn fail(b: *std.Build, message: []const u8) void {
    b.getInstallStep().dependOn(&b.addFail(message).step);
}
