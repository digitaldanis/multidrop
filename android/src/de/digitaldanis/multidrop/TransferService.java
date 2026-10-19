package de.digitaldanis.multidrop;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.database.Cursor;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.*;
import android.provider.*;
import android.security.keystore.*;
import java.io.*;
import java.math.BigInteger;
import java.net.*;
import java.security.*;
import java.util.*;
import java.util.concurrent.*;
import javax.net.ssl.*;
import javax.security.auth.x500.X500Principal;
import org.json.*;

public class TransferService extends Service implements TransferEngine.Host {
  public static volatile TransferService current;
  public static volatile String status = "Bereit zum Teilen";
  public static volatile long progress, total;
  public volatile TransferEngine engine;
  public volatile List<TransferEngine.Item> selected = Collections.emptyList();
  public volatile Offer offer;
  public volatile String browserUrl = "";
  public volatile boolean busy;
  private final ExecutorService work = Executors.newSingleThreadExecutor();
  private WifiManager.MulticastLock multicast;
  private PowerManager.WakeLock wake;
  private final Handler main = new Handler(Looper.getMainLooper());
  private volatile boolean stopped;
  // Conscrypt signs TLS handshakes with raw RSA (NoPadding) through the KeyStore;
  // keys made before that padding was allowed live under the old alias.
  private static final String TLS_KEY = "multidrop-localsend-tls";
  private static final String OLD_TLS_KEY = "multidrop-localsend";

  public static final class Offer {
    public final String sender;
    public final List<TransferEngine.Item> files;
    public final CountDownLatch decision = new CountDownLatch(1);
    public volatile boolean accepted;

    Offer(String sender, List<TransferEngine.Item> files) {
      this.sender = sender;
      this.files = files;
    }

    public void answer(boolean yes) {
      accepted = yes;
      decision.countDown();
    }
  }

  @Override
  public IBinder onBind(Intent i) {
    return null;
  }

  @Override
  public void onCreate() {
    super.onCreate();
    current = this;
  }

  private Notification notification(String text, boolean incoming) {
    PendingIntent open =
        PendingIntent.getActivity(
            this,
            20,
            new Intent(this, MainActivity.class),
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    PendingIntent stop =
        PendingIntent.getService(
            this,
            21,
            new Intent(this, TransferService.class).setAction("STOP"),
            PendingIntent.FLAG_IMMUTABLE);
    return new Notification.Builder(this, incoming ? "offers" : "transfers")
        .setSmallIcon(android.R.drawable.stat_sys_upload_done)
        .setContentTitle(incoming ? "Dateien empfangen?" : "MultiDrop ist sichtbar")
        .setContentText(text)
        .setContentIntent(open)
        .setOngoing(!incoming)
        .setAutoCancel(incoming)
        .addAction(new Notification.Action.Builder(null, "Beenden", stop).build())
        .build();
  }

  @Override
  public int onStartCommand(Intent intent, int flags, int id) {
    if (intent != null && "STOP".equals(intent.getAction())) {
      stopSelf();
      return START_NOT_STICKY;
    }
    NotificationManager nm = getSystemService(NotificationManager.class);
    nm.createNotificationChannel(
        new NotificationChannel(
            "transfers", "Gerätesuche & Dateiübertragung", NotificationManager.IMPORTANCE_LOW));
    nm.createNotificationChannel(
        new NotificationChannel("offers", "Empfangsanfragen", NotificationManager.IMPORTANCE_HIGH));
    startForeground(
        10,
        notification("Im WLAN als " + alias() + " erreichbar", false),
        ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
    if (engine == null && !busy) {
      busy = true;
      work.execute(
          () -> {
            try {
              WifiManager wifi =
                  (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
              multicast = wifi.createMulticastLock("multidrop-files");
              multicast.setReferenceCounted(false);
              multicast.acquire();
              KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
              ks.load(null);
              if (ks.containsAlias(OLD_TLS_KEY)) ks.deleteEntry(OLD_TLS_KEY);
              if (!ks.containsAlias(TLS_KEY)) {
                KeyPairGenerator generator =
                    KeyPairGenerator.getInstance(
                        KeyProperties.KEY_ALGORITHM_RSA, "AndroidKeyStore");
                generator.initialize(
                    new KeyGenParameterSpec.Builder(
                            TLS_KEY,
                            KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_DECRYPT)
                        .setKeySize(2048)
                        .setDigests(
                            KeyProperties.DIGEST_NONE,
                            KeyProperties.DIGEST_SHA256,
                            KeyProperties.DIGEST_SHA512)
                        .setSignaturePaddings(
                            KeyProperties.SIGNATURE_PADDING_RSA_PKCS1,
                            KeyProperties.SIGNATURE_PADDING_RSA_PSS)
                        .setEncryptionPaddings(
                            KeyProperties.ENCRYPTION_PADDING_NONE,
                            KeyProperties.ENCRYPTION_PADDING_RSA_PKCS1,
                            KeyProperties.ENCRYPTION_PADDING_RSA_OAEP)
                        .setCertificateSubject(new X500Principal("CN=MultiDrop Android"))
                        .setCertificateSerialNumber(BigInteger.ONE)
                        .setCertificateNotBefore(new Date(1704067200000L))
                        .setCertificateNotAfter(new Date(2335219200000L))
                        .build());
                generator.generateKeyPair();
              }
              String fingerprint =
                  TransferEngine.hex(
                      MessageDigest.getInstance("SHA-256")
                          .digest(ks.getCertificate(TLS_KEY).getEncoded()));
              KeyManagerFactory km =
                  KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
              km.init(ks, null);
              SSLContext tls = SSLContext.getInstance("TLS");
              tls.init(km.getKeyManagers(), null, new SecureRandom());
              engine = new TransferEngine(alias(), fingerprint, true, this);
              engine.start(tls.getServerSocketFactory(), 53317, true);
              if (stopped) {
                engine.close();
                if (multicast != null && multicast.isHeld()) multicast.release();
                return;
              }
              status = "Sichtbar als " + alias();
            } catch (Exception e) {
              status = "Start fehlgeschlagen: " + e.getMessage();
              main.post(this::stopSelf);
            } finally {
              busy = false;
            }
          });
    }
    return START_NOT_STICKY;
  }

  public String alias() {
    return getSharedPreferences("multidrop", 0).getString("alias", Build.MODEL + " · MultiDrop");
  }

  public void setSelection(List<Uri> uris) {
    if (busy) {
      status = "Bitte den laufenden Vorgang abwarten";
      return;
    }
    busy = true;
    status = "Dateien werden vorbereitet …";
    work.execute(
        () -> {
          try {
            List<TransferEngine.Item> files = new ArrayList<>();
            if (uris.size() > 100) throw new IOException("Bitte höchstens 100 Dateien auswählen");
            for (Uri uri : uris) {
              if (!"content".equals(uri.getScheme()))
                throw new IOException("Bitte eine Datei aus dem Android-Dateimanager wählen");
              String name = "Datei", mime = getContentResolver().getType(uri);
              long size = -1;
              try (Cursor c =
                  getContentResolver()
                      .query(
                          uri,
                          new String[] {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE},
                          null,
                          null,
                          null)) {
                if (c != null && c.moveToFirst()) {
                  int ni = c.getColumnIndex(OpenableColumns.DISPLAY_NAME),
                      si = c.getColumnIndex(OpenableColumns.SIZE);
                  if (ni >= 0) name = c.getString(ni);
                  if (si >= 0 && !c.isNull(si)) size = c.getLong(si);
                }
              }
              TransferEngine.Source source =
                  () -> {
                    InputStream in = getContentResolver().openInputStream(uri);
                    if (in == null) throw new IOException("Datei nicht verfügbar");
                    return in;
                  };
              if (size < 0)
                throw new IOException(
                    "Unbekannte Dateigröße: " + name + ". Bitte zuerst lokal speichern.");
              files.add(
                  new TransferEngine.Item(
                      UUID.randomUUID().toString(), name, mime, size, null, source));
            }
            selected = Collections.unmodifiableList(files);
            status = files.size() + " Datei(en) bereit";
          } catch (Exception e) {
            status = "Auswahl fehlgeschlagen: " + e.getMessage();
          } finally {
            busy = false;
          }
        });
  }

  public void selectText(String text) {
    if (busy) {
      status = "Bitte den laufenden Vorgang abwarten";
      return;
    }
    byte[] bytes = text.getBytes(java.nio.charset.StandardCharsets.UTF_8);
    selected =
        Collections.singletonList(
            new TransferEngine.Item(
                UUID.randomUUID().toString(),
                "Nachricht.txt",
                "text/plain",
                bytes.length,
                null,
                () -> new ByteArrayInputStream(bytes)));
    status = "Text bereit zum Senden";
  }

  public void send(TransferEngine.Peer peer, String pin) {
    if (engine == null || busy || selected.isEmpty()) return;
    busy = true;
    List<TransferEngine.Item> files = selected;
    work.execute(
        () -> {
          acquireWake();
          try {
            engine.send(peer, files, pin);
          } catch (Exception e) {
            status = "Versand fehlgeschlagen: " + e.getMessage();
          } finally {
            busy = false;
            releaseWake();
            progress = total = 0;
          }
        });
  }

  public void shareBrowser(Runnable ready) {
    if (engine == null || selected.isEmpty() || busy) return;
    busy = true;
    work.execute(
        () -> {
          try {
            String address = localAddress();
            if (address.isEmpty()) throw new IOException("Keine lokale WLAN-Adresse");
            int port = engine.shareBrowser(selected);
            browserUrl = "http://" + address + ":" + port + engine.browserPath();
            status = "Browserfreigabe für 15 Minuten aktiv";
            main.post(ready);
          } catch (Exception e) {
            status = "Freigabe fehlgeschlagen: " + e.getMessage();
          } finally {
            busy = false;
          }
        });
  }

  public void stopBrowser() {
    if (engine != null) engine.stopBrowser();
    browserUrl = "";
    status = "Browserfreigabe beendet";
  }

  public String localAddress() {
    android.net.ConnectivityManager connectivity =
        (android.net.ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
    for (android.net.Network network : connectivity.getAllNetworks()) {
      android.net.NetworkCapabilities capabilities = connectivity.getNetworkCapabilities(network);
      if (capabilities == null
          || (!capabilities.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI)
              && !capabilities.hasTransport(android.net.NetworkCapabilities.TRANSPORT_ETHERNET)))
        continue;
      android.net.LinkProperties properties = connectivity.getLinkProperties(network);
      if (properties == null) continue;
      for (android.net.LinkAddress link : properties.getLinkAddresses()) {
        InetAddress address = link.getAddress();
        if (address instanceof Inet4Address && address.isSiteLocalAddress())
          return address.getHostAddress();
      }
    }
    return "";
  }

  @Override
  public boolean accept(String sender, List<TransferEngine.Item> files) throws Exception {
    Offer pending = new Offer(sender, files);
    offer = pending;
    getSystemService(NotificationManager.class)
        .notify(
            11,
            notification(
                sender + " möchte " + files.size() + " Datei(en) senden. Zum Bestätigen öffnen.",
                true));
    try {
      pending.decision.await(90, TimeUnit.SECONDS);
      return pending.accepted && !stopped;
    } finally {
      if (offer == pending) offer = null;
      getSystemService(NotificationManager.class).cancel(11);
    }
  }

  @Override
  public TransferEngine.Sink create(TransferEngine.Item item) throws Exception {
    if (item.size
        > new StatFs(Environment.getExternalStorageDirectory().getPath()).getAvailableBytes())
      throw new IOException("Nicht genug Speicherplatz für " + item.name);
    ContentValues values = new ContentValues();
    values.put(MediaStore.Downloads.DISPLAY_NAME, item.name);
    values.put(MediaStore.Downloads.MIME_TYPE, item.mime);
    values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/MultiDrop");
    values.put(MediaStore.Downloads.IS_PENDING, 1);
    Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
    if (uri == null) throw new IOException("Download konnte nicht angelegt werden");
    PowerManager.WakeLock receiving =
        ((PowerManager) getSystemService(POWER_SERVICE))
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MultiDrop:receive");
    receiving.acquire(30 * 60 * 1000L);
    return new TransferEngine.Sink() {
      public OutputStream output() throws IOException {
        OutputStream out = getContentResolver().openOutputStream(uri, "w");
        if (out == null) throw new IOException("Download nicht beschreibbar");
        return out;
      }

      public void commit() throws IOException {
        ContentValues ready = new ContentValues();
        ready.put(MediaStore.Downloads.IS_PENDING, 0);
        if (getContentResolver().update(uri, ready, null, null) != 1)
          throw new IOException("Download konnte nicht abgeschlossen werden");
        if (receiving.isHeld()) receiving.release();
      }

      public void abort() {
        try {
          getContentResolver().delete(uri, null, null);
        } catch (Exception ignored) {
        }
        if (receiving.isHeld()) receiving.release();
      }
    };
  }

  @Override
  public void event(String message, long done, long size) {
    status = message;
    progress = done;
    total = size;
  }

  @Override
  public synchronized void completed(String name, boolean received) {
    status = (received ? "Empfangen: " : "Gesendet: ") + name;
    progress = total = 0;
    try {
      android.content.SharedPreferences prefs = getSharedPreferences("multidrop", 0);
      JSONArray old = new JSONArray(prefs.getString("history", "[]")), list = new JSONArray();
      list.put(
          new JSONObject()
              .put("name", name)
              .put("received", received)
              .put("time", System.currentTimeMillis()));
      for (int i = 0; i < Math.min(49, old.length()); i++) list.put(old.get(i));
      prefs.edit().putString("history", list.toString()).apply();
    } catch (JSONException ignored) {
    }
  }

  private synchronized void acquireWake() {
    wake =
        ((PowerManager) getSystemService(POWER_SERVICE))
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MultiDrop:files");
    wake.acquire(30 * 60 * 1000L);
  }

  private synchronized void releaseWake() {
    if (wake != null && wake.isHeld()) wake.release();
  }

  @Override
  public void onTimeout(int startId, int fgsType) {
    status = "Sichtbarkeit beendet. App zum Neustarten öffnen.";
    stopSelf();
  }

  @Override
  public void onDestroy() {
    stopped = true;
    if (current == this) current = null;
    if (offer != null) offer.answer(false);
    if (engine != null) engine.close();
    work.shutdownNow();
    releaseWake();
    if (multicast != null && multicast.isHeld()) multicast.release();
    getSystemService(NotificationManager.class).cancel(11);
    stopForeground(STOP_FOREGROUND_REMOVE);
    super.onDestroy();
  }
}
