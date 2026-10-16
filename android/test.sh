#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
: "${JAVA_HOME:?Set JAVA_HOME to JDK 17 or newer}"
mkdir -p build/tests
"$JAVA_HOME/bin/javac" -d build/tests src/de/digitaldanis/multidrop/RaopClient.java tests/RaopClientTest.java
"$JAVA_HOME/bin/java" -cp build/tests RaopClientTest
ffmpeg -v error -y -i build/test-frame.caf -f s16le build/decoded.pcm
cmp build/expected.pcm build/decoded.pcm
printf '%s\n' 'PASS: independent FFmpeg ALAC decoding is bit-exact'
./dependencies.sh --test
if [ ! -f build/test-tls.p12 ]; then
  "$JAVA_HOME/bin/keytool" -genkeypair -alias test -keystore build/test-tls.p12 -storetype PKCS12 -storepass testpass -keypass testpass -keyalg RSA -keysize 2048 -validity 30 -dname 'CN=localhost' >/dev/null 2>&1
fi
"$JAVA_HOME/bin/javac" -cp dependencies/json-20240303.jar -d build/tests src/de/digitaldanis/multidrop/LocalHttp.java src/de/digitaldanis/multidrop/TransferEngine.java tests/TransferEngineTest.java
"$JAVA_HOME/bin/java" -cp build/tests:dependencies/json-20240303.jar de.digitaldanis.multidrop.TransferEngineTest
"$JAVA_HOME/bin/javac" -cp build/tests:dependencies/json-20240303.jar -d build/tests tests/InteropHarness.java
