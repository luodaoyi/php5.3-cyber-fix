#!/usr/bin/env bash
set -euo pipefail
mode=${1:-patched}
case "$mode" in baseline|patched|asan) ;; *) exit 2 ;; esac
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$root/build/$mode" "$root/artifacts/$mode"
if [ "$mode" = baseline ]; then
    git -C "$root" show 3714f35:php-5.3.29/Zend/zend_alloc.c > "$root/build/$mode/zend_alloc.c"
    git -C "$root" show 3714f35:php-5.3.29/sapi/fpm/fpm/fpm_main.c > "$root/build/$mode/fpm_main.c"
fi
cp -a "$root/php-5.3.29" "$root/build/$mode/source"
cd "$root/build/$mode/source"
if [ "$mode" = baseline ]; then
    cp ../zend_alloc.c Zend/zend_alloc.c
    cp ../fpm_main.c sapi/fpm/fpm/fpm_main.c
fi
export CFLAGS='-O2 -g -fcommon'
if [ "$mode" = asan ]; then
    export CFLAGS='-O1 -g -fcommon -fno-omit-frame-pointer -fsanitize=address'
    export LDFLAGS='-fsanitize=address'
    export ASAN_OPTIONS=detect_leaks=0:abort_on_error=1
fi
bash configure --prefix="$root/artifacts/$mode/php" --disable-all --without-pear \
    --disable-cgi --enable-cli --enable-fpm --enable-session --enable-filter --enable-json \
    --enable-mbstring --enable-pdo --with-mysql=mysqlnd --with-mysqli=mysqlnd \
    --with-pdo-mysql=mysqlnd
make -j2
make install
"$root/artifacts/$mode/php/bin/php" -v
python3 "$root/ci/verify.py" "$mode"
