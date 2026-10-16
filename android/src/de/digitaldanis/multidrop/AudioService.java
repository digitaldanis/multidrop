package de.digitaldanis.multidrop;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.media.*;
import android.media.projection.*;
import android.net.wifi.WifiManager;
import android.os.*;
import java.util.concurrent.locks.LockSupport;

public class AudioService extends Service {
  public static volatile String status = "Bereit. Wähle einen Lautsprecher.";
  public static volatile boolean running;
  private volatile boolean stopping;
  private volatile AudioRecord recorder;
  private MediaProjection projection;
  private volatile RaopClient client;
  private PowerManager.WakeLock wake;
  private WifiManager.WifiLock wifiLock;
  private Thread worker;
  private int startId;
  private final Handler main = new Handler(Looper.getMainLooper());

  @Override
  public IBinder onBind(Intent i) {
    return null;
  }

  @Override
  public int onStartCommand(Intent i, int flags, int id) {
    if (i == null) {
      stopSelf();
      return START_NOT_STICKY;
    }
    if ("STOP".equals(i.getAction())) {
      stopSelf();
      return START_NOT_STICKY;
    }
    if (running) return START_NOT_STICKY;
    startId = id;
    boolean tone = i.getBooleanExtra("tone", false);
    NotificationManager nm = getSystemService(NotificationManager.class);
    nm.createNotificationChannel(
        new NotificationChannel("audio", "AirPlay-Audio", NotificationManager.IMPORTANCE_LOW));
    PendingIntent stop =
        PendingIntent.getService(
            this,
            0,
            new Intent(this, AudioService.class).setAction("STOP"),
            PendingIntent.FLAG_IMMUTABLE);
    PendingIntent open =
        PendingIntent.getActivity(
            this, 1, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
    Notification n =
        new Notification.Builder(this, "audio")
            .setContentTitle("MultiDrop Audio")
            .setContentText("AirPlay: " + i.getStringExtra("name"))
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(new Notification.Action.Builder(null, "Stoppen", stop).build())
            .build();
    startForeground(
        1,
        n,
        tone
            ? ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            : ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
    running = true;
    stopping = false;
    status = "Verbinde mit " + i.getStringExtra("name") + " …";
    try {
      wake =
          ((PowerManager) getSystemService(POWER_SERVICE))
              .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MultiDrop:audio");
      wake.acquire();
      wifiLock =
          ((WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE))
              .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "MultiDrop:audio");
      wifiLock.acquire();
      if (!tone) {
        Intent permission = i.getParcelableExtra("projection");
        projection =
            ((MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE))
                .getMediaProjection(i.getIntExtra("result", 0), permission);
        if (projection == null) throw new IllegalStateException("Audiofreigabe fehlt");
        projection.registerCallback(
            new MediaProjection.Callback() {
              @Override
              public void onStop() {
                stopSelf(startId);
              }
            },
            main);
        AudioPlaybackCaptureConfiguration capture =
            new AudioPlaybackCaptureConfiguration.Builder(projection)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                .addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                .build();
        AudioFormat format =
            new AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(RaopClient.RATE)
                .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
                .build();
        int min =
            AudioRecord.getMinBufferSize(
                RaopClient.RATE, AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT);
        if (min < 0) throw new IllegalStateException("44,1-kHz-Stereoaufnahme nicht verfügbar");
        recorder =
            new AudioRecord.Builder()
                .setAudioFormat(format)
                .setBufferSizeInBytes(Math.max(min * 2, RaopClient.FRAMES * 4 * 16))
                .setAudioPlaybackCaptureConfig(capture)
                .build();
        if (recorder.getState() != AudioRecord.STATE_INITIALIZED)
          throw new IllegalStateException("Audioaufnahme konnte nicht gestartet werden");
      }
      worker = new Thread(() -> stream(i, tone), "multidrop-audio");
      worker.start();
    } catch (Exception e) {
      status = "Fehler: " + e.getMessage();
      stopSelf();
    }
    return START_NOT_STICKY;
  }

  private void stream(Intent i, boolean tone) {
    try {
      android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_AUDIO);
      client =
          new RaopClient(
              i.getStringExtra("host"),
              i.getIntExtra("port", 5000),
              i.getBooleanExtra("rsa", false));
      if (stopping) return;
      client.connect();
      if (stopping) return;
      AudioRecord record = recorder;
      if (record != null) record.startRecording();
      status =
          tone
              ? "Testton läuft für 10 Sekunden …"
              : "Übertragung läuft → " + i.getStringExtra("name") + ". Jetzt Musik starten.";
      short[] samples = new short[RaopClient.FRAMES * 2];
      long frame = 0, start = System.nanoTime(), silentFrames = 0;
      while (!stopping) {
        if (tone) {
          if (frame >= RaopClient.RATE * 10L) break;
          for (int f = 0; f < RaopClient.FRAMES; f++) {
            double envelope =
                Math.min(1, (frame + f) / 2205.0)
                    * Math.min(1, (RaopClient.RATE * 10.0 - frame - f) / 2205.0);
            short value =
                (short)
                    (Math.sin(2 * Math.PI * 440 * (frame + f) / RaopClient.RATE) * 2500 * envelope);
            samples[2 * f] = samples[2 * f + 1] = value;
          }
          long wait = start + frame * 1000000000L / RaopClient.RATE - System.nanoTime();
          if (wait > 0) LockSupport.parkNanos(wait);
        } else {
          int offset = 0;
          while (offset < samples.length && !stopping) {
            int n =
                record.read(samples, offset, samples.length - offset, AudioRecord.READ_BLOCKING);
            if (n <= 0) throw new IllegalStateException("Audioaufnahme beendet (" + n + ")");
            offset += n;
          }
          boolean nonzero = false;
          for (short s : samples)
            if (Math.abs((int) s) > 8) {
              nonzero = true;
              break;
            }
          silentFrames = nonzero ? 0 : silentFrames + RaopClient.FRAMES;
          if (silentFrames > RaopClient.RATE * 5L)
            status =
                "Verbunden, aber kein freigegebener Ton. Musik starten; die Quell-App muss"
                    + " Audioaufnahme erlauben.";
          else if (nonzero) status = "Audio wird übertragen → " + i.getStringExtra("name");
        }
        if (stopping) break;
        client.send(samples);
        frame += RaopClient.FRAMES;
      }
      if (!stopping) status = "Testton beendet.";
    } catch (Exception e) {
      if (!stopping) status = "Verbindung fehlgeschlagen: " + e.getMessage();
    } finally {
      if (client != null) client.teardown();
      AudioRecord r = recorder;
      if (r != null) {
        try {
          r.stop();
        } catch (Exception ignored) {
        }
        r.release();
        recorder = null;
      }
      main.post(() -> stopSelf(startId));
    }
  }

  @Override
  public void onDestroy() {
    stopping = true;
    if (status.startsWith("Audio wird")
        || status.startsWith("Übertragung läuft")
        || status.startsWith("Verbunden,")
        || status.startsWith("Verbinde ")
        || status.startsWith("Testton läuft")) status = "Übertragung gestoppt.";
    AudioRecord r = recorder;
    if (r != null)
      try {
        r.stop();
      } catch (Exception ignored) {
      }
    // Closing the sockets cancels a pending connect/read without blocking Android's main thread.
    if (client != null) client.close();
    if (worker != null) worker.interrupt();
    else if (r != null) {
      r.release();
      recorder = null;
    }
    if (projection != null) {
      projection.stop();
      projection = null;
    }
    if (wake != null && wake.isHeld()) wake.release();
    if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
    running = false;
    stopForeground(STOP_FOREGROUND_REMOVE);
    super.onDestroy();
  }
}
