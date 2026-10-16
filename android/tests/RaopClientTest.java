import de.digitaldanis.multidrop.RaopClient;
import java.io.*;
import java.net.*;
import java.nio.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

public class RaopClientTest {
  static void check(boolean b, String m) {
    if (!b) throw new AssertionError(m);
  }

  static long bits(byte[] b, int at, int n) {
    long v = 0;
    for (int i = 0; i < n; i++) v = (v << 1) | ((b[(at + i) / 8] >> (7 - (at + i) % 8)) & 1);
    return v;
  }

  static String line(InputStream in) throws Exception {
    ByteArrayOutputStream b = new ByteArrayOutputStream();
    int c;
    while ((c = in.read()) >= 0 && c != 10) b.write(c);
    if (c < 0) throw new EOFException();
    return b.toString("UTF-8").replace("\r", "");
  }

  static Map<String, String> headers(InputStream in) throws Exception {
    Map<String, String> h = new HashMap<>();
    String s;
    while (!(s = line(in)).isEmpty()) {
      int p = s.indexOf(':');
      h.put(s.substring(0, p).toLowerCase(), s.substring(p + 1).trim());
    }
    return h;
  }

  static DatagramPacket receive(DatagramSocket s) throws Exception {
    DatagramPacket p = new DatagramPacket(new byte[2048], 2048);
    s.receive(p);
    return p;
  }

  static void codec() throws Exception {
    short[] pcm = new short[704];
    Random r = new Random(123);
    for (int i = 0; i < pcm.length; i++) pcm[i] = (short) r.nextInt();
    pcm[0] = Short.MIN_VALUE;
    pcm[1] = Short.MAX_VALUE;
    pcm[2] = 0;
    pcm[3] = -1;
    byte[] data = RaopClient.encodeAlac(pcm, 352);
    check(bits(data, 0, 3) == 1, "stereo element");
    check(bits(data, 19, 1) == 1, "explicit frame size");
    check(bits(data, 22, 1) == 1, "uncompressed ALAC");
    check(bits(data, 23, 32) == 352, "frame count");
    for (int i = 0; i < 704; i++)
      check((short) bits(data, 55 + 16 * i, 16) == pcm[i], "lossless sample " + i);
    check(bits(data, 55 + 704 * 16, 3) == 7, "end element");
    // Export a standard CAF file so FFmpeg can independently validate the ALAC frame.
    try (DataOutputStream out =
        new DataOutputStream(new FileOutputStream("build/test-frame.caf"))) {
      out.writeBytes("caff");
      out.writeShort(1);
      out.writeShort(0);
      out.writeBytes("desc");
      out.writeLong(32);
      out.writeDouble(44100);
      out.writeBytes("alac");
      out.writeInt(0);
      out.writeInt(0);
      out.writeInt(352);
      out.writeInt(2);
      out.writeInt(0);
      out.writeBytes("kuki");
      out.writeLong(24);
      out.writeInt(352);
      out.writeByte(0);
      out.writeByte(16);
      out.writeByte(40);
      out.writeByte(10);
      out.writeByte(14);
      out.writeByte(2);
      out.writeShort(255);
      out.writeInt(0);
      out.writeInt(0);
      out.writeInt(44100);
      out.writeBytes("pakt");
      out.writeLong(26);
      out.writeLong(1);
      out.writeLong(352);
      out.writeInt(0);
      out.writeInt(0);
      out.writeByte(0x80 | (data.length >> 7));
      out.writeByte(data.length & 127);
      out.writeBytes("data");
      out.writeLong(data.length + 4);
      out.writeInt(0);
      out.write(data);
    }
    try (FileOutputStream out = new FileOutputStream("build/expected.pcm")) {
      for (short s : pcm) {
        out.write(s & 255);
        out.write((s >>> 8) & 255);
      }
    }
  }

  static void loopback() throws Exception {
    try (ServerSocket server = new ServerSocket(0);
        DatagramSocket audio = new DatagramSocket(0);
        DatagramSocket control = new DatagramSocket(0);
        DatagramSocket timing = new DatagramSocket(0)) {
      audio.setSoTimeout(5000);
      control.setSoTimeout(5000);
      timing.setSoTimeout(5000);
      CompletableFuture<Void> receiver =
          CompletableFuture.runAsync(
              () -> {
                try (Socket sock = server.accept()) {
                  sock.setSoTimeout(5000);
                  InputStream in = sock.getInputStream();
                  OutputStream out = sock.getOutputStream();
                  int remoteControl = 0;
                  for (String method :
                      new String[] {"ANNOUNCE", "SETUP", "RECORD", "SET_PARAMETER", "TEARDOWN"}) {
                    String request = line(in);
                    check(request.startsWith(method + " "), "method " + request);
                    Map<String, String> h = headers(in);
                    byte[] body = in.readNBytes(Integer.parseInt(h.get("content-length")));
                    String extra = "";
                    if (method.equals("ANNOUNCE"))
                      check(
                          new String(body, StandardCharsets.UTF_8).contains("a=fmtp:96 352 0 16"),
                          "SDP");
                    if (method.equals("SETUP")) {
                      String transport = h.get("transport");
                      Map<String, Integer> ports = new HashMap<>();
                      for (String item : transport.split(";"))
                        if (item.contains("_port=")) {
                          String[] p = item.split("=");
                          ports.put(p[0], Integer.valueOf(p[1]));
                        }
                      remoteControl = ports.get("control_port");
                      ByteBuffer query = ByteBuffer.allocate(32);
                      query.put((byte) 0x80).put((byte) 0xd2);
                      query.position(24);
                      query.putLong(1234567);
                      timing.send(
                          new DatagramPacket(
                              query.array(),
                              32,
                              InetAddress.getLoopbackAddress(),
                              ports.get("timing_port")));
                      DatagramPacket timingReply = receive(timing);
                      check(timingReply.getLength() == 32, "timing length");
                      check(
                          ByteBuffer.wrap(timingReply.getData()).getLong(8) == 1234567,
                          "timing origin");
                      check((timingReply.getData()[1] & 255) == 0xd3, "timing reply");
                      extra =
                          "Session: fake-session\r\nTransport: RTP/AVP/UDP;server_port="
                              + audio.getLocalPort()
                              + ";control_port="
                              + control.getLocalPort()
                              + ";timing_port="
                              + timing.getLocalPort()
                              + "\r\n";
                    }
                    if (method.equals("RECORD"))
                      check("fake-session".equals(h.get("session")), "session header");
                    out.write(
                        ("RTSP/1.0 200 OK\r\nCSeq: "
                                + h.get("cseq")
                                + "\r\n"
                                + extra
                                + "Content-Length: 0\r\n\r\n")
                            .getBytes(StandardCharsets.UTF_8));
                    out.flush();
                    if (method.equals("SET_PARAMETER")) {
                      DatagramPacket p = receive(audio);
                      byte[] first = Arrays.copyOf(p.getData(), p.getLength());
                      int seq = ByteBuffer.wrap(first).getShort(2) & 65535;
                      check((first[1] & 255) == 0xe0, "first marker");
                      check(
                          bits(Arrays.copyOfRange(first, 12, first.length), 23, 32) == 352,
                          "ALAC RTP payload");
                      DatagramPacket sync = receive(control);
                      check(sync.getLength() == 20, "sync length");
                      check((sync.getData()[1] & 255) == 0xd4, "sync type");
                      ByteBuffer requestResend = ByteBuffer.allocate(8);
                      requestResend
                          .put((byte) 0x80)
                          .put((byte) 0xd5)
                          .putShort((short) 1)
                          .putShort((short) seq)
                          .putShort((short) 1);
                      control.send(
                          new DatagramPacket(
                              requestResend.array(),
                              8,
                              InetAddress.getLoopbackAddress(),
                              remoteControl));
                      DatagramPacket resent = receive(control);
                      check((resent.getData()[1] & 255) == 0xd6, "resend type");
                      check(
                          Arrays.equals(
                              first, Arrays.copyOfRange(resent.getData(), 4, resent.getLength())),
                          "resent payload");
                    }
                  }
                } catch (Exception e) {
                  throw new CompletionException(e);
                }
              });
      try (RaopClient client = new RaopClient("127.0.0.1", server.getLocalPort(), false)) {
        client.connect();
        client.send(new short[704]);
        Thread.sleep(200);
        client.teardown();
      }
      receiver.get(10, TimeUnit.SECONDS);
    }
  }

  static void rejectAuth() throws Exception {
    try (ServerSocket server = new ServerSocket(0)) {
      CompletableFuture<Void> receiver =
          CompletableFuture.runAsync(
              () -> {
                try (Socket sock = server.accept()) {
                  InputStream in = sock.getInputStream();
                  line(in);
                  Map<String, String> h = headers(in);
                  in.readNBytes(Integer.parseInt(h.get("content-length")));
                  sock.getOutputStream()
                      .write(
                          "RTSP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n"
                              .getBytes(StandardCharsets.UTF_8));
                } catch (Exception e) {
                  throw new CompletionException(e);
                }
              });
      try (RaopClient c = new RaopClient("127.0.0.1", server.getLocalPort(), true)) {
        try {
          c.connect();
          throw new AssertionError("must reject password receiver");
        } catch (IOException e) {
          check(e.getMessage().contains("Pairing"), "actionable auth error");
        }
      }
      receiver.get(10, TimeUnit.SECONDS);
    }
  }

  public static void main(String[] args) throws Exception {
    codec();
    loopback();
    rejectAuth();
    System.out.println(
        "PASS: ALAC samples; RTSP handshake/session; RTP; timing; sync; retransmit; RSA"
            + " announce/auth rejection");
  }
}
