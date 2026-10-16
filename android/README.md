# MultiDrop for Android

A native Android 10+ sharing app in `android/`, with a local-first interface for
**Share**, **Audio**, and **History**. The desktop Python/GTK application is not
embedded: Android has its own storage, permission, service, and networking adapters.

## What works in this preview

| Feature | Android implementation |
| --- | --- |
| LocalSend send / receive | v2 protocol, multicast discovery, HTTPS, certificate fingerprint checks, explicit receive consent |
| Files and text | Multiple document selection, Android share-target entry point, text/links as UTF-8 files |
| Browser downloads | QR code and random capability URL; expires after 15 minutes or when stopped |
| Quick Share | Android's system share sheet; uses the phone's installed Quick Share provider |
| AirPlay audio | Classic RAOP over UDP, ALAC, optional RSA/AES, test tone and Android playback capture |
| History | Last 50 completed file transfers, stored only on the phone |
| AirDrop | Not directly available: stock Android does not expose the required AWDL radio operations |
| Screen mirroring / AirPlay 2 pairing | Not implemented in this preview |

Quick Share integration is **not** a port of the desktop UKEY2/BLE implementation.
AirPlay receivers that require PIN/password, FairPlay, or modern pairing are not
supported. These limitations are also explained in the app. The UI is currently German.

## Install and use

Download `MultiDrop-Android-preview` from the Android GitHub Actions run, extract
`MultiDrop-Android-0.2.0.apk`, and install it on Android 10 or newer. Android may ask
to permit installation from your download source. This is a signed development
preview, not a store release. CI preview signing keys are ephemeral; installing a
later CI build can require uninstalling the earlier preview.

1. Put both devices on the same local network and open MultiDrop or LocalSend.
2. Select files, choose a nearby device, and approve on the receiver.
3. Received files appear in `Downloads/MultiDrop`. Files are only published after
   the announced size and optional SHA-256 checksum match. Failed partial files
   are deleted, and existing files are not intentionally overwritten (MediaStore
   allocates a unique destination).
4. To share with a browser, select files and choose **Im Browser teilen**. Scan the
   QR code on another device or copy the link. Anyone with the link on that network
   can download the selected files until it expires or you stop sharing.
5. For sound, open **Audio**, select an AirPlay receiver, try the test tone, then
   approve Android's playback-capture prompt. Select the whole screen if Android
   offers a single-app / whole-screen choice. Only audio is transmitted.

The receiving service starts while the app is open and stays visible in a
notification. Every incoming batch needs user consent (90-second decision timeout).
Use **Sichtbarkeit beenden** or the notification's stop action to close the servers.
Android 15 limits background data-sync foreground services; on timeout the service
stops and can be restarted by opening the app. No boot receiver, cloud service,
account, broad storage permission, or automatic incoming acceptance is used.

Files must have a known length and a safe basename. Directory trees are not
currently supported; select the files themselves. A batch is limited to 100 files.
Document-provider access must remain available while sending or browser-sharing.
Text is transferred as `Nachricht.txt`, rather than a separate chat interface.

## Audio limitations

- Android only exposes playback from apps that permit audio capture. DRM content,
  calls, and apps that opt out remain silent. The microphone is never recorded,
  although Android requires `RECORD_AUDIO` permission for playback capture.
- Expect roughly two seconds of latency. Local phone playback is not automatically
  muted; muting media volume can also silence the captured audio.
- Receiver volume starts at -15 dB. The test tone is quiet and ends after ten seconds.
- Locking the screen can revoke capture on recent Android versions.
- Real AirPlay hardware still needs interoperability testing; automated RAOP tests
  do not establish compatibility with a particular speaker.

## Android Studio / Gradle

Open this directory in Android Studio. The project uses AGP 8.7.3, Gradle 8.9,
JDK 17, compile/target SDK 35, and minimum SDK 29.

```bash
./gradlew :app:assembleDebug
# app/build/outputs/apk/debug/app-debug.apk
```

ZXing core 3.5.3 is the only Android runtime dependency, used to generate QR codes.
The Gradle distribution is pinned by SHA-256. `org.json` is supplied by Android.

## Lightweight command-line build

The checked-in SDK build script uses the same sources/resources and works without
Gradle. It needs JDK 17+, Python 3, curl, unzip, and the Android SDK:

```bash
sdkmanager 'platforms;android-35' 'build-tools;35.0.0'
export ANDROID_HOME=/path/to/android-sdk
export JAVA_HOME=/path/to/jdk-17
./build.sh
# build/MultiDrop-Android-0.2.0.apk
```

`dependencies.sh` fetches the pinned ZXing jar and verifies its SHA-256. The build
compiles Java, creates DEX, aligns the APK, signs it, and verifies the signature.
The local development key is stored outside the repository in
`~/.android/multidrop-development.jks` (override with `MULTIDROP_KEYSTORE`). Preserve
that private key for compatible updates. The default Gradle debug build uses a
different key. Production releases need a stable private release-signing process.

The application ID remains `de.digitaldanis.multidrop.audio` so locally signed
builds can update the earlier audio prototype when signed with the same key.

## Validation

```bash
# JVM protocol tests + independent FFmpeg ALAC decoding
JAVA_HOME=/path/to/jdk-17 ./test.sh

# From repository root, with the Python package installed:
JAVA_HOME=/path/to/jdk-17 python android/tests/test_desktop_interop.py
```

Tests cover:

- LocalSend send/receive, consent rejection, session contention, invalid filenames,
  sizes, hashes, incorrect tokens, cancellation, retry, replay, and chunked uploads;
- TLS certificate pinning, browser-link authorization and HTML escaping;
- malformed/ambiguous HTTP framing;
- RTSP negotiation, ALAC sample fidelity, timing/sync packets and RTP retransmits;
- byte-exact decoding of the sender's ALAC frame by FFmpeg;
- both directions against the repository's real Python LocalSend backend.

`.github/workflows/android.yml` builds the APK and runs those tests. A second job
boots an API 35 emulator, starts the actual Android KeyStore-backed HTTPS receiver,
checks navigation, accepts an incoming transfer in the UI, verifies the MediaStore
file and history, and uploads screenshots/logs. This does not replace real-device
Wi-Fi, background-lifecycle, Quick Share, or AirPlay-speaker testing.

## Code map

- `MainActivity`: native sharing hub, document picker, system sharing, QR, consent, history.
- `TransferService`: Android KeyStore TLS identity, notifications, MediaStore, lifecycle.
- `TransferEngine`: platform-independent LocalSend, discovery, sessions, browser downloads.
- `LocalHttp`: bounded streaming HTTP server, including chunked bodies.
- `AirplayActivity`, `AudioService`, `RaopClient`: discovery, playback capture, RAOP transport.
- `tests/`: protocol, desktop interoperability, and emulator smoke tests.

LocalSend defaults to HTTPS with a per-install Android KeyStore certificate; outgoing
HTTPS peers must match their advertised SHA-256 fingerprint. Discovery is unauthenticated
LAN discovery, so a matching fingerprint identifies the discovered certificate, not a
person. Plain HTTP LocalSend peers remain supported and are labeled in the UI. Browser
sharing intentionally uses HTTP on an ephemeral local port and a 192-bit random URL,
without exposing filesystem paths. It serves only the explicitly selected files.

GPL-3.0-or-later; see `../LICENSE`. Third-party notices and license texts are bundled
under `assets/` and accessible from the app's About dialog.
