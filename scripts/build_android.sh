#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

commit="$(git rev-parse HEAD 2>/dev/null || printf local)"
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || printf local)"
build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat > build_info.py <<EOF
APP_COMMIT = "$commit"
APP_BRANCH = "$branch"
APP_BUILD_TIME = "$build_time"
EOF

export P4A_RELEASE_KEYSTORE="${P4A_RELEASE_KEYSTORE:-$PWD/android/footlive.keystore}"
export P4A_RELEASE_KEYSTORE_PASSWD="${P4A_RELEASE_KEYSTORE_PASSWD:-footlive-direct}"
export P4A_RELEASE_KEYALIAS="${P4A_RELEASE_KEYALIAS:-footlive}"
export P4A_RELEASE_KEYALIAS_PASSWD="${P4A_RELEASE_KEYALIAS_PASSWD:-footlive-direct}"

if [[ ! -f "$P4A_RELEASE_KEYSTORE" ]]; then
    mkdir -p "$(dirname "$P4A_RELEASE_KEYSTORE")"
    keytool -genkeypair -v \
        -keystore "$P4A_RELEASE_KEYSTORE" \
        -storepass "$P4A_RELEASE_KEYSTORE_PASSWD" \
        -alias "$P4A_RELEASE_KEYALIAS" \
        -keypass "$P4A_RELEASE_KEYALIAS_PASSWD" \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=Foot Live Direct APK, OU=Foot Live, O=Wiriath, L=Paris, C=FR"
fi

buildozer android release
apk="$(find bin -maxdepth 1 -type f -name '*release*.apk' | head -1)"
test -n "$apk"
cp "$apk" bin/FootLive.apk
printf 'Built %s\n' "$PWD/bin/FootLive.apk"
