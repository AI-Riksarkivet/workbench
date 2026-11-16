#!/bin/sh
# Assemble a relocatable PostgreSQL server tree from the official image into /out, for the offline suite
# (`tests/postgres.py`) and the local stack (`scripts/local-stack.sh`): the server's own tree as the image lays it out
# (`usr/lib/postgresql/<major>`, `usr/share/postgresql/<major>`, so the binaries find their share directory by the same
# relative path), the shared libraries it links beyond the C library in `deps/`, and `bin` pointing at the binaries.
# Run inside the image by `make postgres` and `dagger call test`, never on a host. The JIT (LLVM) is left out: it is
# a third of the tree and nothing here turns it on.
set -eu
major=$(ls /usr/lib/postgresql)
out=/out
mkdir -p "$out/usr/lib/postgresql" "$out/usr/share/postgresql" "$out/deps"
cp -a "/usr/lib/postgresql/$major" "$out/usr/lib/postgresql/"
rm -rf "$out/usr/lib/postgresql/$major/lib/llvmjit"* "$out/usr/lib/postgresql/$major/lib/bitcode"
# Dereferenced: the image links `postgresql.conf.sample` out of the share directory.
cp -aL "/usr/share/postgresql/$major" "$out/usr/share/postgresql/"
for f in "$out/usr/lib/postgresql/$major"/bin/* "$out/usr/lib/postgresql/$major"/lib/*.so; do
  ldd "$f" 2>/dev/null | awk '/=> \//{print $3}'
done | sort -u | while read -r lib; do
  case "$(basename "$lib")" in
    libc.so*|libm.so*|ld-linux*|libpthread.so*|libdl.so*|librt.so*|libresolv.so*) ;;
    *) cp -L "$lib" "$out/deps/" ;;
  esac
done
ln -s "usr/lib/postgresql/$major/bin" "$out/bin"
