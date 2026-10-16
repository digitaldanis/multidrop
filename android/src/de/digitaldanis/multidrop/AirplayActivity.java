package de.digitaldanis.multidrop;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.projection.MediaProjectionManager;
import android.net.nsd.*;
import android.net.wifi.WifiManager;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class AirplayActivity extends Activity {
  private final Handler handler = new Handler(Looper.getMainLooper());
  private final LinkedHashMap<String, NsdServiceInfo> devices = new LinkedHashMap<>();
  private final ArrayDeque<NsdServiceInfo> resolveQueue = new ArrayDeque<>();
  private boolean resolving, destroyed, discovering;
  private NsdManager nsd;
  private WifiManager.MulticastLock multicast;
  private LinearLayout list;
  private TextView status;
  private NsdServiceInfo selected;
  private final Runnable refresh =
      new Runnable() {
        public void run() {
          status.setText(AudioService.status);
          handler.postDelayed(this, 700);
        }
      };

  private int dp(int n) {
    return (int) (n * getResources().getDisplayMetrics().density);
  }

  private TextView text(String s, int size) {
    TextView t = new TextView(this);
    t.setText(s);
    t.setTextSize(size);
    t.setTextColor(Color.rgb(37, 32, 57));
    t.setPadding(0, dp(8), 0, dp(8));
    return t;
  }

  private void button(LinearLayout parent, String label, Runnable run) {
    Button b = new Button(this);
    b.setText(label);
    b.setAllCaps(false);
    b.setOnClickListener(v -> run.run());
    parent.addView(b);
  }

  @Override
  public void onCreate(Bundle b) {
    super.onCreate(b);
    ScrollView scroll = new ScrollView(this);
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(1);
    root.setPadding(dp(24), dp(24), dp(24), dp(32));
    root.setBackgroundColor(0xfff5f3fa);
    scroll.addView(root);
    setContentView(scroll);
    scroll.setOnApplyWindowInsetsListener(
        (v, insets) -> {
          if (Build.VERSION.SDK_INT >= 30) {
            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
          } else
            v.setPadding(
                insets.getSystemWindowInsetLeft(),
                insets.getSystemWindowInsetTop(),
                insets.getSystemWindowInsetRight(),
                insets.getSystemWindowInsetBottom());
          return insets;
        });
    button(root, "‹ Zurück", this::finish);
    root.addView(text("AirPlay Audio", 30));
    root.addView(text("Dein Sound. Auf deinen Lautsprechern.", 18));
    root.addView(
        text(
            "Handy und Lautsprecher müssen im selben WLAN sein. Wähle einen Empfänger und erlaube"
                + " die Audiofreigabe. Falls Android fragt, wähle den gesamten Bildschirm.",
            15));
    status = text(AudioService.status, 16);
    root.addView(status);
    button(root, "Übertragung stoppen", () -> stopService(new Intent(this, AudioService.class)));
    root.addView(text("Lautsprecher im WLAN", 21));
    list = new LinearLayout(this);
    list.setOrientation(1);
    root.addView(list);
    button(root, "Erneut suchen", this::restartDiscovery);
    root.addView(
        text(
            "Experimentell · Android 10+\n"
                + "Klassisches AirPlay/RAOP über UDP, ALAC und RSA. AirPlay-2-Pairing, PIN und"
                + " Passwort werden noch nicht unterstützt.\n\n"
                + "Nur freigegebener Medien- und Spielton kann übertragen werden. Geschützte"
                + " Inhalte und Apps mit Aufnahmesperre bleiben stumm. Kein Mikrofon wird"
                + " aufgenommen. AirPlay hat etwa 2 Sekunden Verzögerung.",
            14));
    button(
        root,
        "Projekt & Lizenz",
        () ->
            new AlertDialog.Builder(this)
                .setTitle("MultiDrop Audio · GPL-3.0-or-later")
                .setMessage(
                    "Experimentelle, eigenständige Android-Portierung des Audio-Anwendungsfalls von"
                        + " digitaldanis/multidrop. Nicht vom Originalprojekt veröffentlicht.\n\n"
                        + "RAOP-Protokollreferenz: PipeWire (MIT). Quellcode und Lizenztexte liegen"
                        + " dem Download bei.\n\n"
                        + "https://github.com/digitaldanis/multidrop")
                .setPositiveButton("OK", null)
                .show());
    nsd = (NsdManager) getSystemService(NSD_SERVICE);
    WifiManager wifi = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
    multicast = wifi.createMulticastLock("multidrop-discovery");
    multicast.setReferenceCounted(false);
    multicast.acquire();
    restartDiscovery();
    handler.post(refresh);
    if (Build.VERSION.SDK_INT >= 33
        && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED)
      requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 3);
  }

  private String attr(NsdServiceInfo s, String key) {
    byte[] b = s.getAttributes().get(key);
    return b == null ? "" : new String(b, StandardCharsets.UTF_8);
  }

  private void render() {
    list.removeAllViews();
    if (devices.isEmpty()) list.addView(text("Suche nach AirPlay-Audio-Empfängern …", 15));
    for (NsdServiceInfo s : devices.values()) {
      String name = s.getServiceName().replaceFirst("^[^@]*@", "");
      button(list, name, () -> choose(s));
    }
  }

  private void choose(NsdServiceInfo s) {
    if (AudioService.running) {
      Toast.makeText(this, "Bitte zuerst die laufende Übertragung stoppen.", Toast.LENGTH_LONG)
          .show();
      return;
    }
    selected = s;
    new AlertDialog.Builder(this)
        .setTitle(s.getServiceName().replaceFirst("^[^@]*@", ""))
        .setItems(
            new String[] {"Handy-Audio übertragen", "Leisen Testton senden (10 Sekunden)"},
            (d, which) -> {
              if ("true".equalsIgnoreCase(attr(s, "pw"))) {
                error("Dieser Empfänger verlangt ein Passwort. Das wird noch nicht unterstützt.");
                return;
              }
              String et = attr(s, "et"), cn = attr(s, "cn"), tp = attr(s, "tp");
              if (!et.isEmpty()
                  && !Arrays.asList(et.split(",")).contains("0")
                  && !Arrays.asList(et.split(",")).contains("1")) {
                error(
                    "Dieser Empfänger benötigt AirPlay-Pairing/FairPlay. Diese Version unterstützt"
                        + " klassisches RAOP (ohne Verschlüsselung oder RSA).");
                return;
              }
              if (!cn.isEmpty() && !Arrays.asList(cn.split(",")).contains("1")) {
                error("Der Empfänger bietet kein ALAC an.");
                return;
              }
              if (!tp.isEmpty() && !tp.toUpperCase(Locale.ROOT).contains("UDP")) {
                error("Dieser Empfänger bietet kein RAOP über UDP an.");
                return;
              }
              if (which == 1) start(true, 0, null);
              else capture();
            })
        .show();
  }

  private void error(String message) {
    new AlertDialog.Builder(this).setMessage(message).setPositiveButton("OK", null).show();
  }

  private void capture() {
    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
        != PackageManager.PERMISSION_GRANTED) {
      requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, 1);
      return;
    }
    startActivityForResult(
        ((MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE))
            .createScreenCaptureIntent(),
        2);
  }

  @Override
  public void onRequestPermissionsResult(int r, String[] p, int[] g) {
    super.onRequestPermissionsResult(r, p, g);
    if (r == 1 && g.length > 0 && g[0] == PackageManager.PERMISSION_GRANTED) capture();
  }

  @Override
  protected void onActivityResult(int req, int result, Intent data) {
    super.onActivityResult(req, result, data);
    if (req == 2 && result == RESULT_OK && data != null) start(false, result, data);
  }

  private void start(boolean tone, int result, Intent data) {
    if (selected == null) return;
    Intent i =
        new Intent(this, AudioService.class)
            .putExtra("host", selected.getHost().getHostAddress())
            .putExtra("port", selected.getPort())
            .putExtra("name", selected.getServiceName().replaceFirst("^[^@]*@", ""))
            .putExtra("rsa", Arrays.asList(attr(selected, "et").split(",")).contains("1"))
            .putExtra("tone", tone)
            .putExtra("result", result)
            .putExtra("projection", data);
    startForegroundService(i);
  }

  private void restartDiscovery() {
    if (discovering) {
      try {
        nsd.stopServiceDiscovery(listener);
      } catch (Exception ignored) {
      }
      return;
    }
    devices.clear();
    render();
    discover();
  }

  private void discover() {
    try {
      nsd.discoverServices("_raop._tcp.", NsdManager.PROTOCOL_DNS_SD, listener);
      discovering = true;
    } catch (Exception e) {
      status.setText("Suche fehlgeschlagen: " + e.getMessage());
    }
  }

  private final NsdManager.DiscoveryListener listener =
      new NsdManager.DiscoveryListener() {
        public void onDiscoveryStarted(String t) {}

        public void onDiscoveryStopped(String t) {
          handler.post(
              () -> {
                discovering = false;
                if (!destroyed) {
                  devices.clear();
                  render();
                  discover();
                }
              });
        }

        public void onStartDiscoveryFailed(String t, int c) {
          handler.post(
              () -> {
                discovering = false;
                list.removeAllViews();
                list.addView(text("WLAN-Suche fehlgeschlagen (" + c + "). Erneut versuchen.", 15));
              });
        }

        public void onStopDiscoveryFailed(String t, int c) {}

        public void onServiceFound(NsdServiceInfo s) {
          handler.post(
              () -> {
                resolveQueue.add(s);
                resolveNext();
              });
        }

        public void onServiceLost(NsdServiceInfo s) {
          handler.post(
              () -> {
                devices.remove(s.getServiceName());
                render();
              });
        }
      };

  private void resolveNext() {
    if (destroyed || resolving || resolveQueue.isEmpty()) return;
    resolving = true;
    NsdServiceInfo next = resolveQueue.remove();
    nsd.resolveService(
        next,
        new NsdManager.ResolveListener() {
          public void onResolveFailed(NsdServiceInfo s, int c) {
            handler.post(
                () -> {
                  resolving = false;
                  resolveNext();
                });
          }

          public void onServiceResolved(NsdServiceInfo s) {
            handler.post(
                () -> {
                  if (!destroyed && s.getHost() != null) {
                    devices.put(s.getServiceName(), s);
                    render();
                  }
                  resolving = false;
                  resolveNext();
                });
          }
        });
  }

  @Override
  public void onDestroy() {
    destroyed = true;
    handler.removeCallbacks(refresh);
    if (discovering)
      try {
        nsd.stopServiceDiscovery(listener);
      } catch (Exception ignored) {
      }
    if (multicast != null && multicast.isHeld()) multicast.release();
    super.onDestroy();
  }
}
