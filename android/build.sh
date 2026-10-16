#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${ANDROID_HOME:?Set ANDROID_HOME to an Android SDK with platform 35 and build-tools 35.0.0}"
: "${JAVA_HOME:?Set JAVA_HOME to a JDK 17 or newer}"
export PATH="$JAVA_HOME/bin:$PATH"
BT="$ANDROID_HOME/build-tools/35.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-35/android.jar"
./dependencies.sh
mkdir -p build/classes build/dex build/generated build/assets
cp assets/* build/assets/
"$BT/aapt2" compile --dir res -o build/resources.zip
python3 - <<'PYBUILD'
import xml.etree.ElementTree as ET
ET.register_namespace('android', 'http://schemas.android.com/apk/res/android')
manifest = ET.parse('AndroidManifest.xml')
manifest.getroot().set('package', 'de.digitaldanis.multidrop.audio')
manifest.write('build/AndroidManifest.xml', encoding='utf-8', xml_declaration=True)
PYBUILD
"$BT/aapt2" link -o build/resources.apk --manifest build/AndroidManifest.xml -I "$PLATFORM" --java build/generated -A build/assets build/resources.zip
find src build/generated -name '*.java' -print > build/sources.txt
javac --release 8 -classpath "$PLATFORM:dependencies/zxing-core-3.5.3.jar" -d build/classes @build/sources.txt
jar cf build/classes.jar -C build/classes .
"$BT/d8" --lib "$PLATFORM" --min-api 29 --output build/dex build/classes.jar dependencies/zxing-core-3.5.3.jar
cp build/resources.apk build/unsigned.apk
python3 - <<'PY'
import zipfile
with zipfile.ZipFile('build/unsigned.apk','a',compression=zipfile.ZIP_DEFLATED) as z:
    z.write('build/dex/classes.dex','classes.dex')
PY
"$BT/zipalign" -f -p 4 build/unsigned.apk build/aligned.apk
# Local development key, intentionally excluded from the source archive.
KEYSTORE="${MULTIDROP_KEYSTORE:-$HOME/.android/multidrop-development.jks}"
mkdir -p "$(dirname "$KEYSTORE")"
if [ ! -f "$KEYSTORE" ]; then
    keytool -genkeypair -keystore "$KEYSTORE" -storepass android -keypass android -alias multidrop -keyalg RSA -keysize 3072 -validity 10000 -dname "CN=MultiDrop Audio Experimental"
    chmod 600 "$KEYSTORE"
fi
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-key-alias multidrop --ks-pass pass:android --key-pass pass:android --out build/MultiDrop-Android-0.2.0.apk build/aligned.apk
"$BT/apksigner" verify --verbose build/MultiDrop-Android-0.2.0.apk
"$BT/zipalign" -c -p 4 build/MultiDrop-Android-0.2.0.apk
