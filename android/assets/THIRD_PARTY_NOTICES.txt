MultiDrop Audio Android is an experimental independent Android implementation of
the AirPlay audio use case of https://github.com/digitaldanis/multidrop.
This Android implementation is licensed under GPL-3.0-or-later; see ../LICENSE.
It is not an official upstream release. No affiliation with Apple.

The RAOP packet formats, uncompressed ALAC packing, and public RSA modulus were
referenced against PipeWire's src/modules/module-raop-sink.c:
https://github.com/PipeWire/pipewire/blob/master/src/modules/module-raop-sink.c

Copyright © 2021 Wim Taymans
SPDX-License-Identifier: MIT

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

QR generation uses ZXing core 3.5.3, Copyright ZXing authors, Apache-2.0.
https://github.com/zxing/zxing
The full license is bundled as assets/ZXING-LICENSE.txt.

The Gradle wrapper comes from Gradle 8.9 (Apache-2.0).
https://github.com/gradle/gradle/tree/v8.9.0

org.json 20240303 is used only for JVM tests (Android provides org.json at runtime).
https://github.com/stleary/JSON-java (public domain).
