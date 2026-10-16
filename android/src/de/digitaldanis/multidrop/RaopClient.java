package de.digitaldanis.multidrop;

import java.io.*;
import java.math.BigInteger;
import java.net.*;
import java.nio.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.spec.RSAPublicKeySpec;
import java.util.*;
import javax.crypto.Cipher;
import javax.crypto.spec.*;

/** Classic RAOP UDP sender. Protocol layout referenced against PipeWire's MIT RAOP sink. */
public final class RaopClient implements AutoCloseable {
  public static final int RATE = 44100, FRAMES = 352;
  private static final String MODULUS =
      "59dE8qLieItsH1WgjrcFRKj6eUWqi+bGLOX1HL3U3GhC/j0Qg90u3sG/1CUtwC5vOYvfDmFI6oSFXi5ELabWJmT2dKHzBJKa3k9ok+8t9ucRqMd6DZHJ2YCCLlDRKSKv6kDqnw4UwPdpOMXziC/AMj3Z/lUVX1G7WSHCAWKf1zNS1eLvqr+boEjXuBOitnZ/bDzPHrTOZz0Dew0uowxf/+sG+NCK3eQJVxqcaJ/vEHKIVd2M+5qL71yJQ+87X6oV3eaYvt3zWZYD6z5vYTcrtij2VZ9Zmni/UAaHqn9JdsBWLUEpVviYnhimNVvYFZeCXg/IdTQ+x4IRdiXNv5hEew==";
  private final SecureRandom random = new SecureRandom();
  private final InetAddress host;
  private final int port;
  private final boolean encrypted;
  private Socket rtsp;
  private InputStream in;
  private OutputStream out;
  private DatagramSocket audio, control, timing;
  private int audioPort,
      controlPort,
      sequence = random.nextInt(65536),
      timestamp = random.nextInt(),
      ssrc = random.nextInt(),
      cseq = 1;
  private int packets, latency = 88200;
  private String uri, session;
  private final String instance = Long.toHexString(random.nextLong()).toUpperCase(Locale.ROOT);
  private byte[] key, iv;
  private Cipher aes;
  private volatile boolean closed;
  private final byte[][] history = new byte[512][];
  private volatile IOException backgroundError;

  public RaopClient(String address, int port, boolean encrypted) throws IOException {
    this.host = InetAddress.getByName(address);
    this.port = port;
    this.encrypted = encrypted;
  }

  public void connect() throws Exception {
    rtsp = new Socket();
    rtsp.connect(new InetSocketAddress(host, port), 5000);
    rtsp.setSoTimeout(5000);
    rtsp.setTcpNoDelay(true);
    in = rtsp.getInputStream();
    out = rtsp.getOutputStream();
    audio = new DatagramSocket();
    control = new DatagramSocket();
    timing = new DatagramSocket();
    control.setSoTimeout(1000);
    timing.setSoTimeout(1000);
    String local = rtsp.getLocalAddress().getHostAddress(),
        family = host instanceof Inet6Address ? "IP6" : "IP4";
    uri =
        "rtsp://"
            + (local.contains(":") ? "[" + local + "]" : local)
            + "/"
            + (random.nextInt() & 0xffffffffL);
    String sdp =
        "v=0\r\no=iTunes "
            + (random.nextInt() & 0xffffffffL)
            + " 0 IN "
            + family
            + " "
            + local
            + "\r\ns=MultiDrop\r\nc=IN "
            + family
            + " "
            + host.getHostAddress()
            + "\r\n"
            + "t=0 0\r\n"
            + "m=audio 0 RTP/AVP 96\r\n"
            + "a=rtpmap:96 AppleLossless\r\n"
            + "a=fmtp:96 352 0 16 40 10 14 2 255 0 0 44100\r\n";
    Map<String, String> headers = new LinkedHashMap<>();
    if (encrypted) {
      key = new byte[16];
      iv = new byte[16];
      random.nextBytes(key);
      random.nextBytes(iv);
      PublicKey pk =
          KeyFactory.getInstance("RSA")
              .generatePublic(
                  new RSAPublicKeySpec(
                      new BigInteger(1, Base64.getDecoder().decode(MODULUS)),
                      BigInteger.valueOf(65537)));
      Cipher rsa = Cipher.getInstance("RSA/ECB/OAEPWithSHA-1AndMGF1Padding");
      rsa.init(Cipher.ENCRYPT_MODE, pk);
      Base64.Encoder b64 = Base64.getEncoder().withoutPadding();
      sdp +=
          "a=rsaaeskey:"
              + b64.encodeToString(rsa.doFinal(key))
              + "\r\na=aesiv:"
              + b64.encodeToString(iv)
              + "\r\n";
      byte[] challenge = new byte[16];
      random.nextBytes(challenge);
      headers.put("Apple-Challenge", b64.encodeToString(challenge));
      aes = Cipher.getInstance("AES/CBC/NoPadding");
    }
    headers.put("Content-Type", "application/sdp");
    request("ANNOUNCE", headers, sdp);
    startTiming();
    Map<String, String> setup =
        request(
            "SETUP",
            Collections.singletonMap(
                "Transport",
                "RTP/AVP/UDP;unicast;mode=record;control_port="
                    + control.getLocalPort()
                    + ";timing_port="
                    + timing.getLocalPort()),
            null);
    session = setup.get("session");
    if (session != null) session = session.split(";")[0];
    String transport = setup.getOrDefault("transport", "");
    audioPort = transportPort(transport, "server_port");
    controlPort = transportPort(transport, "control_port");
    if (audioPort == 0 || controlPort == 0)
      throw new IOException("Empfänger liefert keine RAOP-UDP-Ports.");
    Map<String, String> recordHeaders = new LinkedHashMap<>();
    recordHeaders.put("Range", "npt=0-");
    recordHeaders.put("RTP-Info", "seq=" + sequence + ";rtptime=" + (timestamp & 0xffffffffL));
    Map<String, String> record = request("RECORD", recordHeaders, null);
    try {
      latency = Integer.parseInt(record.getOrDefault("audio-latency", "88200"));
    } catch (NumberFormatException ignored) {
    }
    // Start at a moderate receiver volume; do not send at maximum volume.
    request(
        "SET_PARAMETER",
        Collections.singletonMap("Content-Type", "text/parameters"),
        "volume: -15.000000\r\n");
    startResends();
  }

  private static int transportPort(String transport, String key) {
    for (String item : transport.split(";"))
      if (item.trim().startsWith(key + "="))
        try {
          return Integer.parseInt(item.trim().substring(key.length() + 1));
        } catch (NumberFormatException ignored) {
        }
    return 0;
  }

  private String line() throws IOException {
    ByteArrayOutputStream b = new ByteArrayOutputStream();
    for (int c; (c = in.read()) != -1; ) {
      if (c == '\n') return b.toString("UTF-8").replace("\r", "");
      b.write(c);
      if (b.size() > 8192) throw new IOException("RTSP-Zeile zu lang");
    }
    throw new EOFException("Empfänger hat die Verbindung beendet.");
  }

  private synchronized Map<String, String> request(
      String method, Map<String, String> headers, String body) throws IOException {
    if (closed) throw new IOException("Übertragung gestoppt");
    byte[] bytes = body == null ? new byte[0] : body.getBytes(StandardCharsets.UTF_8);
    StringBuilder h =
        new StringBuilder(
            method
                + " "
                + uri
                + " RTSP/1.0\r\nCSeq: "
                + cseq++
                + "\r\nUser-Agent: iTunes/12.0 (MultiDrop Android)\r\nClient-Instance: "
                + instance
                + "\r\nDACP-ID: "
                + instance
                + "\r\nActive-Remote: 1\r\n");
    if (session != null) h.append("Session: ").append(session).append("\r\n");
    for (Map.Entry<String, String> e : headers.entrySet())
      h.append(e.getKey()).append(": ").append(e.getValue()).append("\r\n");
    h.append("Content-Length: ").append(bytes.length).append("\r\n\r\n");
    out.write(h.toString().getBytes(StandardCharsets.UTF_8));
    out.write(bytes);
    out.flush();
    String status = line();
    Map<String, String> response = new HashMap<>();
    for (String l; !(l = line()).isEmpty(); ) {
      int idx = l.indexOf(':');
      if (idx > 0)
        response.put(l.substring(0, idx).toLowerCase(Locale.ROOT), l.substring(idx + 1).trim());
      if (response.size() > 100) throw new IOException("Ungültige RTSP-Antwort");
    }
    int length;
    try {
      length = Integer.parseInt(response.getOrDefault("content-length", "0"));
    } catch (NumberFormatException e) {
      throw new IOException("Ungültige RTSP-Länge", e);
    }
    if (length < 0 || length > 1048576) throw new IOException("Ungültige RTSP-Länge");
    byte[] discard = new byte[4096];
    while (length > 0) {
      int n = in.read(discard, 0, Math.min(length, discard.length));
      if (n < 0) throw new EOFException();
      length -= n;
    }
    if (!status.matches("RTSP/1.0 2[0-9][0-9].*"))
      throw new IOException(
          method
              + ": "
              + status
              + (status.contains("401") || status.contains("403") || status.contains("470")
                  ? " · Empfänger verlangt Passwort oder Pairing (hier nicht unterstützt)."
                  : ""));
    return response;
  }

  public static byte[] encodeAlac(short[] pcm, int frames) {
    if (frames < 1 || pcm.length < frames * 2) throw new IllegalArgumentException();
    BitWriter w = new BitWriter(frames * 4 + 8);
    w.put(1, 3);
    w.put(0, 4);
    w.put(0, 8);
    w.put(0, 4);
    w.put(1, 1);
    w.put(0, 2);
    w.put(1, 1);
    w.put(frames, 32);
    for (int i = 0; i < frames * 2; i++) w.put(pcm[i] & 65535, 16);
    w.put(7, 3);
    return Arrays.copyOf(w.bytes, (w.bit + 7) / 8);
  }

  private static class BitWriter {
    final byte[] bytes;
    int bit;

    BitWriter(int n) {
      bytes = new byte[n];
    }

    void put(int value, int count) {
      for (int i = count - 1; i >= 0; i--, bit++)
        bytes[bit / 8] |= ((value >>> i) & 1) << (7 - bit % 8);
    }
  }

  public void send(short[] pcm) throws Exception {
    if (closed) throw new IOException("Übertragung gestoppt");
    if (backgroundError != null) throw backgroundError;
    if (packets % 125 == 0) sync(packets == 0);
    byte[] payload = encodeAlac(pcm, FRAMES);
    if (encrypted) {
      aes.init(Cipher.ENCRYPT_MODE, new SecretKeySpec(key, "AES"), new IvParameterSpec(iv));
      int n = payload.length / 16 * 16;
      byte[] encryptedBytes = aes.doFinal(payload, 0, n);
      System.arraycopy(encryptedBytes, 0, payload, 0, n);
    }
    ByteBuffer packet = ByteBuffer.allocate(12 + payload.length).order(ByteOrder.BIG_ENDIAN);
    packet
        .put((byte) 0x80)
        .put((byte) (packets == 0 ? 0xe0 : 0x60))
        .putShort((short) sequence)
        .putInt(timestamp)
        .putInt(ssrc)
        .put(payload);
    byte[] data = packet.array();
    synchronized (history) {
      history[sequence % history.length] = data;
    }
    audio.send(new DatagramPacket(data, data.length, host, audioPort));
    sequence = (sequence + 1) & 65535;
    timestamp += FRAMES;
    packets++;
    if (packets % 3750 == 0) request("OPTIONS", Collections.emptyMap(), null);
  }

  private static long ntp() {
    long now = System.currentTimeMillis();
    return ((now / 1000 + 2208988800L) << 32) | ((now % 1000) * 0x100000000L / 1000);
  }

  private void sync(boolean first) throws IOException {
    ByteBuffer b = ByteBuffer.allocate(20).order(ByteOrder.BIG_ENDIAN);
    b.put((byte) (first ? 0x90 : 0x80))
        .put((byte) 0xd4)
        .putShort((short) 7)
        .putInt(timestamp - latency)
        .putLong(ntp())
        .putInt(timestamp);
    control.send(new DatagramPacket(b.array(), 20, host, controlPort));
  }

  private void startTiming() {
    Thread t =
        new Thread(
            () -> {
              byte[] data = new byte[1500];
              while (!closed)
                try {
                  DatagramPacket p = new DatagramPacket(data, data.length);
                  timing.receive(p);
                  long received = ntp();
                  if (!p.getAddress().equals(host) || p.getLength() < 32 || (data[1] & 127) != 82)
                    continue;
                  ByteBuffer b = ByteBuffer.allocate(32).order(ByteOrder.BIG_ENDIAN);
                  b.put((byte) 0x80).put((byte) 0xd3).putShort((short) 0).putInt(0);
                  b.put(data, 24, 8).putLong(received).putLong(ntp());
                  timing.send(new DatagramPacket(b.array(), 32, p.getAddress(), p.getPort()));
                } catch (SocketTimeoutException ignored) {
                } catch (IOException e) {
                  if (!closed) backgroundError = e;
                  break;
                }
            },
            "raop-timing");
    t.setDaemon(true);
    t.start();
  }

  private void startResends() {
    Thread t =
        new Thread(
            () -> {
              byte[] data = new byte[1500];
              int resendSeq = 0;
              while (!closed)
                try {
                  DatagramPacket p = new DatagramPacket(data, data.length);
                  control.receive(p);
                  if (!p.getAddress().equals(host) || p.getLength() < 8 || (data[1] & 127) != 85)
                    continue;
                  ByteBuffer b = ByteBuffer.wrap(data).order(ByteOrder.BIG_ENDIAN);
                  int first = b.getShort(4) & 65535, count = Math.min(b.getShort(6) & 65535, 512);
                  for (int i = 0; i < count; i++) {
                    int seq = (first + i) & 65535;
                    byte[] original;
                    synchronized (history) {
                      original = history[seq % history.length];
                    }
                    if (original == null || (ByteBuffer.wrap(original).getShort(2) & 65535) != seq)
                      continue;
                    ByteBuffer reply = ByteBuffer.allocate(4 + original.length);
                    reply
                        .put((byte) 0x80)
                        .put((byte) 0xd6)
                        .putShort((short) resendSeq++)
                        .put(original);
                    control.send(
                        new DatagramPacket(
                            reply.array(), reply.capacity(), p.getAddress(), p.getPort()));
                  }
                } catch (SocketTimeoutException ignored) {
                } catch (IOException e) {
                  if (!closed) backgroundError = e;
                  break;
                }
            },
            "raop-resend");
    t.setDaemon(true);
    t.start();
  }

  public void teardown() {
    try {
      if (rtsp != null && !closed) {
        rtsp.setSoTimeout(1000);
        request("TEARDOWN", Collections.emptyMap(), null);
      }
    } catch (Exception ignored) {
    } finally {
      close();
    }
  }

  @Override
  public void close() {
    closed = true;
    if (audio != null) audio.close();
    if (control != null) control.close();
    if (timing != null) timing.close();
    if (rtsp != null)
      try {
        rtsp.close();
      } catch (IOException ignored) {
      }
  }
}
