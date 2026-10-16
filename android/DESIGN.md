# Android interface

The sharing hub uses three destinations: Share, Audio, and History. A lavender
surface, purple actions, rounded cards, and the existing MultiDrop broadcast/down-arrow
mark distinguish interactive controls from status. The app uses native Android text,
buttons, dialogs, document picking, notifications, and the share sheet; no web UI is
embedded.

The main path is choose files → select a nearby device → receiver confirms. Browser
sharing is an alternative for devices without LocalSend. Receiving requests show file
names and total size before acceptance. AirPlay is separated because it has an ongoing
capture lifecycle and different permissions. Unsupported protocols are described in
About instead of being presented as working controls.

System bars use edge-to-edge insets on API 35 and a compatible path on Android 10.
Content scrolls, text uses scale-independent units, and the bottom navigation stays
reachable. Emulator screenshots are uploaded by the Android smoke workflow.
