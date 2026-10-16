package de.digitaldanis.multidrop;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.*;
import android.graphics.drawable.*;
import android.net.Uri;
import android.os.*;
import android.view.*;
import android.widget.*;
import com.google.zxing.*;
import com.google.zxing.common.BitMatrix;
import java.text.DateFormat;
import java.util.*;
import org.json.*;

/** Native, edge-to-edge Android sharing hub. All transfers remain on the local network. */
public class MainActivity extends Activity {
  private static final int INK = 0xff252039,
      MUTED = 0xff797187,
      PURPLE = 0xff7354d8,
      BG = 0xfff5f3fa;
  private final Handler handler = new Handler(Looper.getMainLooper());
  private LinearLayout body, nav, root;
  private TextView state, selectionCount;
  private ProgressBar transferProgress;
  private LinearLayout peerList;
  private String peerSignature = "", selectionSignature = "";
  private int tab;
  private TransferService.Offer showingOffer;
  private AlertDialog incomingDialog;
  private ArrayList<Uri> selectedUris = new ArrayList<>();
  private boolean resumed;
  private String selectedText = "";

  private int dp(float n) {
    return (int) (n * getResources().getDisplayMetrics().density + .5f);
  }

  private GradientDrawable shape(int color, int radius) {
    GradientDrawable d = new GradientDrawable();
    d.setColor(color);
    d.setCornerRadius(dp(radius));
    return d;
  }

  private TextView text(String label, int sp, int color, boolean bold) {
    TextView t = new TextView(this);
    t.setText(label);
    t.setTextSize(sp);
    t.setTextColor(color);
    t.setFontFeatureSettings("kern");
    if (bold) t.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
    t.setLineSpacing(dp(3), 1);
    return t;
  }

  private LinearLayout column() {
    LinearLayout l = new LinearLayout(this);
    l.setOrientation(LinearLayout.VERTICAL);
    return l;
  }

  private void gap(LinearLayout p, int h) {
    View v = new View(this);
    p.addView(v, new LinearLayout.LayoutParams(1, dp(h)));
  }

  private LinearLayout card(LinearLayout parent, int color) {
    LinearLayout c = column();
    c.setPadding(dp(20), dp(20), dp(20), dp(20));
    c.setBackground(shape(color, 24));
    LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, -2);
    lp.bottomMargin = dp(14);
    parent.addView(c, lp);
    return c;
  }

  private void action(
      LinearLayout p, String title, String subtitle, String icon, Runnable onClick) {
    LinearLayout row = new LinearLayout(this);
    row.setGravity(Gravity.CENTER_VERTICAL);
    row.setPadding(dp(14), dp(15), dp(14), dp(15));
    row.setBackground(
        new RippleDrawable(ColorStateList.valueOf(0x227354d8), shape(0xffffffff, 18), null));
    TextView mark = text(icon, 24, PURPLE, true);
    mark.setGravity(Gravity.CENTER);
    mark.setBackground(shape(0xfff0eafa, 14));
    row.addView(mark, new LinearLayout.LayoutParams(dp(48), dp(48)));
    LinearLayout labels = column();
    LinearLayout.LayoutParams ll = new LinearLayout.LayoutParams(0, -2, 1);
    ll.leftMargin = dp(14);
    row.addView(labels, ll);
    labels.addView(text(title, 16, INK, true));
    if (!subtitle.isEmpty()) labels.addView(text(subtitle, 12, MUTED, false));
    TextView arrow = text("›", 26, MUTED, false);
    row.addView(arrow);
    row.setOnClickListener(v -> onClick.run());
    row.setFocusable(true);
    row.setContentDescription(title + ". " + subtitle);
    LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(-1, -2);
    rp.bottomMargin = dp(10);
    p.addView(row, rp);
  }

  private void button(LinearLayout p, String label, boolean primary, Runnable onClick) {
    Button b = new Button(this);
    b.setText(label);
    b.setTextSize(14);
    b.setAllCaps(false);
    b.setTextColor(primary ? Color.WHITE : PURPLE);
    b.setBackgroundTintList(ColorStateList.valueOf(primary ? PURPLE : 0xffece6fa));
    b.setMinHeight(dp(52));
    b.setOnClickListener(v -> onClick.run());
    p.addView(b, new LinearLayout.LayoutParams(-1, -2));
  }

  @Override
  public void onCreate(Bundle saved) {
    super.onCreate(saved);
    if (saved != null) {
      tab = saved.getInt("tab");
      selectedText = saved.getString("text", "");
      ArrayList<Uri> uris = saved.getParcelableArrayList("uris");
      if (uris != null) selectedUris = uris;
    }
    build();
    ensureService();
    receiveIntent(getIntent());
    if (Build.VERSION.SDK_INT >= 33
        && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED)
      requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 7);
  }

  private void build() {
    root = column();
    root.setBackgroundColor(BG);
    setContentView(root);
    root.setOnApplyWindowInsetsListener(
        (v, insets) -> {
          if (Build.VERSION.SDK_INT >= 30) {
            Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom);
          } else
            v.setPadding(
                insets.getSystemWindowInsetLeft(),
                insets.getSystemWindowInsetTop(),
                insets.getSystemWindowInsetRight(),
                insets.getSystemWindowInsetBottom());
          return insets;
        });
    LinearLayout header = new LinearLayout(this);
    header.setGravity(Gravity.CENTER_VERTICAL);
    header.setPadding(dp(24), dp(16), dp(20), dp(12));
    TextView brand = text("◈  multidrop", 24, INK, true);
    header.addView(brand, new LinearLayout.LayoutParams(0, -2, 1));
    TextView settings = text("•••", 24, PURPLE, true);
    settings.setPadding(dp(16), dp(8), dp(12), dp(8));
    settings.setContentDescription("Einstellungen");
    settings.setOnClickListener(v -> settings());
    header.addView(settings);
    root.addView(header);
    ScrollView scroll = new ScrollView(this);
    scroll.setFillViewport(true);
    scroll.setClipToPadding(false);
    body = column();
    body.setPadding(dp(24), dp(12), dp(24), dp(24));
    scroll.addView(body);
    root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
    nav = new LinearLayout(this);
    nav.setPadding(dp(12), dp(8), dp(12), dp(8));
    root.addView(nav);
    String[] labels = {"◈  Teilen", "♫  Audio", "↺  Verlauf"};
    for (int i = 0; i < 3; i++) {
      final int target = i;
      TextView n = text(labels[i], 14, i == tab ? PURPLE : MUTED, i == tab);
      n.setGravity(Gravity.CENTER);
      n.setPadding(0, dp(14), 0, dp(14));
      if (i == tab) n.setBackground(shape(0xffe9e1f8, 18));
      n.setOnClickListener(
          v -> {
            tab = target;
            build();
          });
      nav.addView(n, new LinearLayout.LayoutParams(0, -2, 1));
    }
    state = null;
    peerList = null;
    selectionCount = null;
    transferProgress = null;
    if (tab == 0) sharing();
    else if (tab == 1) audio();
    else history();
  }

  private void sharing() {
    TextView eyebrow = text("ALLES IN DEINER NÄHE", 11, PURPLE, true);
    eyebrow.setLetterSpacing(.12f);
    body.addView(eyebrow);
    gap(body, 8);
    body.addView(text("Einfach rüber.", 34, INK, true));
    gap(body, 6);
    body.addView(text("Dateien, Fotos und mehr. Direkt von Gerät zu Gerät.", 15, MUTED, false));
    gap(body, 22);
    LinearLayout hero = card(body, PURPLE);
    hero.addView(text("Bereit zum Teilen?", 22, Color.WHITE, true));
    gap(hero, 6);
    selectionCount = text("Wähle aus, was du senden möchtest.", 14, 0xffece5ff, false);
    hero.addView(selectionCount);
    gap(hero, 14);
    Button pick = new Button(this);
    pick.setText("＋  Dateien auswählen");
    pick.setAllCaps(false);
    pick.setTextColor(PURPLE);
    pick.setBackgroundTintList(ColorStateList.valueOf(Color.WHITE));
    pick.setOnClickListener(v -> pick());
    hero.addView(pick, new LinearLayout.LayoutParams(-1, dp(54)));
    LinearLayout live = card(body, 0xffebe7f3);
    state = text(TransferService.status, 14, INK, true);
    live.addView(state);
    transferProgress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
    transferProgress.setProgressTintList(ColorStateList.valueOf(PURPLE));
    live.addView(transferProgress, new LinearLayout.LayoutParams(-1, dp(6)));
    transferProgress.setVisibility(View.GONE);
    TextView nearby = text("Geräte in der Nähe", 20, INK, true);
    body.addView(nearby);
    gap(body, 6);
    body.addView(text("MultiDrop oder LocalSend auf dem anderen Gerät öffnen.", 13, MUTED, false));
    gap(body, 12);
    peerList = column();
    body.addView(peerList);
    peerSignature = "";
    refreshPeers(true);
    action(
        body,
        "Text oder Link senden",
        "Als Textdatei auf dem anderen Gerät",
        "≡",
        this::textDialog);
    action(
        body,
        "Im Browser teilen",
        "Ohne App · Link und QR-Code",
        "↗",
        () -> {
          TransferService s = service();
          if (s == null || s.selected.isEmpty()) {
            toast("Zuerst Dateien auswählen");
            return;
          }
          s.shareBrowser(
              () -> {
                if (!isFinishing() && !isDestroyed()) browserDialog();
              });
        });
    action(
        body, "Quick Share & andere Apps", "Androids Teilen-Menü öffnen", "⇄", this::systemShare);
    action(
        body,
        "Dateien empfangen",
        "Mit Bestätigung · Downloads/MultiDrop",
        "↓",
        () -> {
          ensureService();
          new AlertDialog.Builder(this)
              .setTitle("Empfang bereit")
              .setMessage(
                  "Auf dem anderen Gerät MultiDrop oder LocalSend öffnen und dieses Handy"
                      + " auswählen. Jede Anfrage muss hier bestätigt werden.\n\n"
                      + "Dateien landen in Downloads/MultiDrop.")
              .setPositiveButton("Verstanden", null)
              .show();
        });
    gap(body, 4);
    body.addView(text("Gleiches WLAN. Kein Konto. Keine Cloud.", 12, MUTED, false));
    updateState();
  }

  private void audio() {
    body.addView(text("Dein Sound.\nMehr Raum.", 34, INK, true));
    gap(body, 10);
    body.addView(
        text("Übertrage freigegebenen App-Ton an AirPlay-Lautsprecher im WLAN.", 15, MUTED, false));
    gap(body, 24);
    LinearLayout hero = card(body, 0xffe5edef);
    TextView note = text("♫", 70, 0xff24676c, true);
    note.setGravity(Gravity.CENTER);
    hero.addView(note);
    hero.addView(text("AirPlay Audio", 24, INK, true));
    gap(hero, 8);
    state = text(AudioService.status, 14, MUTED, false);
    hero.addView(state);
    gap(hero, 16);
    button(
        hero,
        "Lautsprecher auswählen",
        true,
        () -> startActivity(new Intent(this, AirplayActivity.class)));
    button(
        hero,
        "Übertragung stoppen",
        false,
        () -> stopService(new Intent(this, AudioService.class)));
    action(
        body,
        "Was lässt sich übertragen?",
        "App-Ton, Freigabe und Kompatibilität",
        "ⓘ",
        () ->
            new AlertDialog.Builder(this)
                .setTitle("AirPlay auf Android")
                .setMessage(
                    "Klassisches AirPlay/RAOP über UDP, mit ALAC und optional RSA/AES. Starte zum"
                        + " Prüfen zuerst den Testton.\n\n"
                        + "Android erlaubt nur den Ton von Apps, die Audioaufnahme freigeben."
                        + " Geschützte Inhalte und Anrufe bleiben stumm. Etwa zwei Sekunden"
                        + " Verzögerung sind normal.\n\n"
                        + "AirPlay-2-Pairing, PIN, Passwort und Bildschirmspiegelung sind noch"
                        + " nicht implementiert.")
                .setPositiveButton("OK", null)
                .show());
  }

  private void history() {
    body.addView(text("Schon angekommen.", 30, INK, true));
    gap(body, 8);
    body.addView(text("Deine letzten 50 abgeschlossenen Dateiübertragungen.", 14, MUTED, false));
    gap(body, 22);
    try {
      JSONArray list =
          new JSONArray(getSharedPreferences("multidrop", 0).getString("history", "[]"));
      if (list.length() == 0) {
        LinearLayout empty = card(body, Color.WHITE);
        empty.addView(text("↺", 48, PURPLE, true));
        empty.addView(text("Hier beginnt dein Verlauf", 20, INK, true));
        gap(empty, 8);
        empty.addView(
            text(
                "Sobald du eine Datei sendest oder empfängst, erscheint sie hier.",
                14,
                MUTED,
                false));
      }
      for (int i = 0; i < list.length(); i++) {
        JSONObject j = list.getJSONObject(i);
        LinearLayout card = card(body, Color.WHITE);
        card.addView(
            text((j.getBoolean("received") ? "↓  " : "↑  ") + j.getString("name"), 16, INK, true));
        card.addView(
            text(
                (j.getBoolean("received") ? "Empfangen" : "Gesendet")
                    + " · "
                    + DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT)
                        .format(new Date(j.getLong("time"))),
                12,
                MUTED,
                false));
      }
    } catch (JSONException ignored) {
    }
    button(
        body,
        "Downloads öffnen",
        false,
        () -> {
          try {
            startActivity(new Intent(DownloadManager.ACTION_VIEW_DOWNLOADS));
          } catch (ActivityNotFoundException e) {
            toast("Dateien liegen in Downloads/MultiDrop");
          }
        });
  }

  private TransferService service() {
    return TransferService.current;
  }

  private void ensureService() {
    if (service() == null)
      try {
        startForegroundService(new Intent(this, TransferService.class));
      } catch (Exception e) {
        toast("Empfang konnte nicht gestartet werden: " + e.getMessage());
      }
  }

  private void pick() {
    Intent i =
        new Intent(Intent.ACTION_OPEN_DOCUMENT)
            .setType("*/*")
            .addCategory(Intent.CATEGORY_OPENABLE)
            .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
    startActivityForResult(i, 1);
  }

  @Override
  protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (request == 1 && result == RESULT_OK && data != null) {
      ArrayList<Uri> uris = uris(data, false);
      for (Uri uri : uris)
        try {
          getContentResolver()
              .takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        } catch (SecurityException ignored) {
        }
      select(uris);
    }
  }

  private ArrayList<Uri> uris(Intent i, boolean shared) {
    ArrayList<Uri> result = new ArrayList<>();
    if (i.getClipData() != null) {
      for (int n = 0; n < Math.min(100, i.getClipData().getItemCount()); n++) {
        Uri u = i.getClipData().getItemAt(n).getUri();
        if (u != null) result.add(u);
      }
    } else if (shared) {
      if (Intent.ACTION_SEND_MULTIPLE.equals(i.getAction())) {
        ArrayList<Uri> all = i.getParcelableArrayListExtra(Intent.EXTRA_STREAM);
        if (all != null) result.addAll(all);
      } else {
        Uri u = i.getParcelableExtra(Intent.EXTRA_STREAM);
        if (u != null) result.add(u);
      }
    } else if (i.getData() != null) result.add(i.getData());
    return result;
  }

  private void select(ArrayList<Uri> uris) {
    if (uris.isEmpty()) return;
    selectedUris = uris;
    selectedText = "";
    ensureService();
    trySelect(uris, 0);
  }

  private void trySelect(ArrayList<Uri> uris, int attempt) {
    TransferService s = service();
    if (s != null && s.engine != null && !s.busy) {
      s.setSelection(uris);
      return;
    }
    if (attempt < 40) handler.postDelayed(() -> trySelect(uris, attempt + 1), 250);
    else toast("Dienst nicht bereit. Bitte erneut auswählen.");
  }

  @Override
  protected void onNewIntent(Intent i) {
    super.onNewIntent(i);
    setIntent(i);
    receiveIntent(i);
  }

  private void receiveIntent(Intent i) {
    if (i == null) return;
    String action = i.getAction();
    if (Intent.ACTION_SEND.equals(action) || Intent.ACTION_SEND_MULTIPLE.equals(action)) {
      ArrayList<Uri> uris = uris(i, true);
      if (!uris.isEmpty()) {
        tab = 0;
        build();
        select(uris);
      } else if (i.hasExtra(Intent.EXTRA_TEXT)) {
        CharSequence text = i.getCharSequenceExtra(Intent.EXTRA_TEXT);
        if (text != null) selectText(text.toString(), 0);
      }
      i.setAction(Intent.ACTION_MAIN);
    }
  }

  private void textDialog() {
    EditText edit = new EditText(this);
    edit.setHint("Text oder Link eingeben");
    edit.setMinLines(3);
    edit.setGravity(Gravity.TOP);
    edit.setPadding(dp(24), dp(16), dp(24), dp(16));
    new AlertDialog.Builder(this)
        .setTitle("Text teilen")
        .setView(edit)
        .setNegativeButton("Abbrechen", null)
        .setPositiveButton("Übernehmen", (d, w) -> selectText(edit.getText().toString(), 0))
        .show();
  }

  private void selectText(String value, int attempt) {
    if (value.isEmpty()) return;
    if (value.length() > 500000) {
      toast("Text ist zu lang");
      return;
    }
    ensureService();
    TransferService s = service();
    if (s != null && s.engine != null && !s.busy) {
      selectedText = value;
      selectedUris.clear();
      s.selectText(value);
    } else if (attempt < 40) handler.postDelayed(() -> selectText(value, attempt + 1), 250);
    else toast("Dienst nicht bereit");
  }

  private void systemShare() {
    if (!selectedText.isEmpty()) {
      startActivity(
          Intent.createChooser(
              new Intent(Intent.ACTION_SEND)
                  .setType("text/plain")
                  .putExtra(Intent.EXTRA_TEXT, selectedText),
              "Text teilen"));
      return;
    }
    if (selectedUris.isEmpty()) {
      toast("Zuerst Dateien auswählen");
      return;
    }
    Intent i =
        new Intent(selectedUris.size() == 1 ? Intent.ACTION_SEND : Intent.ACTION_SEND_MULTIPLE)
            .setType("*/*")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
    if (selectedUris.size() == 1) i.putExtra(Intent.EXTRA_STREAM, selectedUris.get(0));
    else i.putParcelableArrayListExtra(Intent.EXTRA_STREAM, selectedUris);
    ClipData clip = ClipData.newUri(getContentResolver(), "MultiDrop", selectedUris.get(0));
    for (int n = 1; n < selectedUris.size(); n++)
      clip.addItem(new ClipData.Item(selectedUris.get(n)));
    i.setClipData(clip);
    startActivity(Intent.createChooser(i, "Mit Quick Share oder anderer App senden"));
  }

  private void refreshPeers(boolean force) {
    TransferService s = service();
    List<TransferEngine.Peer> peers =
        s == null || s.engine == null ? Collections.emptyList() : s.engine.peers();
    StringBuilder sig = new StringBuilder();
    for (TransferEngine.Peer p : peers) sig.append(p.fingerprint).append(p.address).append(p.name);
    String signature = sig.toString();
    if (!force && signature.equals(peerSignature)) return;
    peerSignature = signature;
    if (peerList == null) return;
    peerList.removeAllViews();
    if (peers.isEmpty()) {
      LinearLayout empty = card(peerList, Color.WHITE);
      empty.addView(text("Noch niemand in Sicht", 16, INK, true));
      empty.addView(
          text(
              "Beide Geräte im selben WLAN? LocalSend oder MultiDrop muss auf dem Empfänger"
                  + " geöffnet sein.",
              13,
              MUTED,
              false));
      button(
          empty,
          "Erneut suchen",
          false,
          () -> {
            ensureService();
            TransferService active = service();
            if (active != null && active.engine != null)
              new Thread(active.engine::announce).start();
          });
    }
    for (TransferEngine.Peer p : peers)
      action(
          peerList,
          p.name,
          (p.scheme.equals("https") ? "Verschlüsselt" : "Unverschlüsseltes HTTP")
              + " · "
              + p.address,
          "◈",
          () -> sendDialog(p));
  }

  private void sendDialog(TransferEngine.Peer p) {
    TransferService s = service();
    if (s == null || s.selected.isEmpty()) {
      toast("Zuerst Dateien auswählen");
      return;
    }
    EditText pin = new EditText(this);
    pin.setSingleLine();
    pin.setHint("Empfänger-PIN (falls eingerichtet)");
    pin.setInputType(
        android.text.InputType.TYPE_CLASS_NUMBER
            | android.text.InputType.TYPE_NUMBER_VARIATION_PASSWORD);
    LinearLayout box = column();
    box.setPadding(dp(24), dp(8), dp(24), 0);
    box.addView(text(s.selected.size() + " Datei(en) an " + p.name + " senden?", 16, INK, false));
    box.addView(pin);
    new AlertDialog.Builder(this)
        .setTitle("Dateien senden")
        .setView(box)
        .setNegativeButton("Abbrechen", null)
        .setPositiveButton("Senden", (d, w) -> s.send(p, pin.getText().toString()))
        .show();
  }

  private void browserDialog() {
    TransferService s = service();
    if (s == null || s.browserUrl.isEmpty()) return;
    String url = s.browserUrl;
    LinearLayout box = column();
    box.setPadding(dp(24), dp(12), dp(24), dp(8));
    try {
      BitMatrix qr = new MultiFormatWriter().encode(url, BarcodeFormat.QR_CODE, 640, 640);
      Bitmap bitmap = Bitmap.createBitmap(640, 640, Bitmap.Config.ARGB_8888);
      for (int y = 0; y < 640; y++)
        for (int x = 0; x < 640; x++) bitmap.setPixel(x, y, qr.get(x, y) ? INK : Color.WHITE);
      ImageView image = new ImageView(this);
      image.setImageBitmap(bitmap);
      image.setContentDescription("QR-Code zur Browserfreigabe");
      box.addView(image, new LinearLayout.LayoutParams(-1, dp(240)));
    } catch (Exception ignored) {
    }
    TextView address = text(url, 12, MUTED, false);
    address.setTextIsSelectable(true);
    box.addView(address);
    gap(box, 10);
    box.addView(
        text(
            "Im selben WLAN scannen. Der Link gilt 15 Minuten. Jeder mit dem Link kann die"
                + " ausgewählten Dateien laden. Browser-Downloads nutzen HTTP.",
            13,
            MUTED,
            false));
    new AlertDialog.Builder(this)
        .setTitle("Ohne App empfangen")
        .setView(box)
        .setPositiveButton(
            "Link kopieren",
            (d, w) -> {
              ((ClipboardManager) getSystemService(CLIPBOARD_SERVICE))
                  .setPrimaryClip(ClipData.newPlainText("MultiDrop", url));
              toast("Link kopiert");
            })
        .setNeutralButton("Freigabe stoppen", (d, w) -> s.stopBrowser())
        .setNegativeButton("Schließen", null)
        .show();
  }

  private void settings() {
    String[] options = {
      "Gerätenamen ändern",
      "Browserfreigabe anzeigen / stoppen",
      "Aktuellen Versand abbrechen",
      "Sichtbarkeit beenden",
      "Verlauf löschen",
      "Über MultiDrop & Protokolle"
    };
    new AlertDialog.Builder(this)
        .setTitle("Dein MultiDrop")
        .setItems(
            options,
            (d, n) -> {
              TransferService s = service();
              switch (n) {
                case 0:
                  rename();
                  break;
                case 1:
                  if (s != null && !s.browserUrl.isEmpty()) browserDialog();
                  else toast("Keine Browserfreigabe aktiv");
                  break;
                case 2:
                  if (s != null && s.engine != null) s.engine.cancelSend();
                  break;
                case 3:
                  stopService(new Intent(this, TransferService.class));
                  TransferService.status = "Offline · Empfang über die Gerätekarte aktivieren";
                  break;
                case 4:
                  getSharedPreferences("multidrop", 0).edit().remove("history").apply();
                  if (tab == 2) build();
                  break;
                case 5:
                  about();
                  break;
              }
            })
        .show();
  }

  private void rename() {
    EditText name = new EditText(this);
    name.setSingleLine();
    name.setText(
        getSharedPreferences("multidrop", 0).getString("alias", Build.MODEL + " · MultiDrop"));
    new AlertDialog.Builder(this)
        .setTitle("Gerätename")
        .setMessage("Wird beim nächsten Start der Sichtbarkeit übernommen.")
        .setView(name)
        .setNegativeButton("Abbrechen", null)
        .setPositiveButton(
            "Speichern",
            (d, w) -> {
              String value = name.getText().toString().trim();
              if (!value.isEmpty() && value.length() <= 64)
                getSharedPreferences("multidrop", 0).edit().putString("alias", value).apply();
              else toast("Bitte 1–64 Zeichen verwenden");
            })
        .show();
  }

  private void about() {
    new AlertDialog.Builder(this)
        .setTitle("MultiDrop für Android · Preview")
        .setMessage(
            "LocalSend: Senden und Empfangen mit Bestätigung, HTTPS und Zertifikatsprüfung.\n\n"
                + "Browser: Download-Link und QR-Code.\n\n"
                + "Quick Share: über das Android-Teilen-Menü, sofern auf dem Handy vorhanden. Kein"
                + " eigener Quick-Share-Protokollstapel.\n\n"
                + "AirPlay: klassischer Audio-Sender. Kein AirPlay-2-Pairing und keine"
                + " Bildschirmspiegelung.\n\n"
                + "AirDrop: direkt auf normalen Android-Geräten nicht verfügbar. Für iPhones"
                + " LocalSend oder den Browser-Link nutzen.\n\n"
                + "GPL-3.0-or-later · digitaldanis/multidrop\n"
                + "QR-Code: ZXing (Apache-2.0)\n"
                + "RAOP-Referenz: PipeWire (MIT)")
        .setPositiveButton("OK", null)
        .setNeutralButton("Lizenzen", (d, w) -> licenses())
        .show();
  }

  private void licenses() {
    try {
      StringBuilder all = new StringBuilder();
      for (String path :
          new String[] {"THIRD_PARTY_NOTICES.txt", "LICENSE.txt", "ZXING-LICENSE.txt"})
        try (java.io.InputStream in = getAssets().open(path)) {
          java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
          byte[] b = new byte[4096];
          int n;
          while ((n = in.read(b)) != -1) out.write(b, 0, n);
          all.append(out.toString("UTF-8")).append("\n\n");
        }
      new AlertDialog.Builder(this)
          .setTitle("Open-Source-Lizenzen")
          .setMessage(all.toString())
          .setPositiveButton("Schließen", null)
          .show();
    } catch (Exception e) {
      toast("Lizenztexte im Quellcode verfügbar");
    }
  }

  private void updateState() {
    if (state != null) state.setText(tab == 1 ? AudioService.status : TransferService.status);
    TransferService s = service();
    if (tab == 0) {
      if (transferProgress != null) {
        long total = TransferService.total;
        transferProgress.setVisibility(total > 0 ? View.VISIBLE : View.GONE);
        if (total > 0) transferProgress.setProgress((int) (TransferService.progress * 100 / total));
      }
      if (selectionCount != null) {
        int count = s == null ? 0 : s.selected.size();
        String value =
            count == 0
                ? "Wähle aus, was du senden möchtest."
                : count + " Datei(en) ausgewählt · bereit zum Senden";
        selectionCount.setText(value);
      }
      refreshPeers(false);
    }
    TransferService.Offer pending = s == null ? null : s.offer;
    if (pending != null && pending != showingOffer && resumed) {
      showingOffer = pending;
      StringBuilder names = new StringBuilder(pending.sender + " möchte senden:\n\n");
      long size = 0;
      for (int i = 0; i < pending.files.size(); i++) {
        TransferEngine.Item f = pending.files.get(i);
        size += f.size;
        if (i < 8) names.append(f.name).append('\n');
      }
      if (pending.files.size() > 8)
        names.append("… und ").append(pending.files.size() - 8).append(" weitere\n");
      names.append("\nGesamt: ").append(android.text.format.Formatter.formatFileSize(this, size));
      incomingDialog =
          new AlertDialog.Builder(this)
              .setTitle("Dateien empfangen?")
              .setMessage(names.toString())
              .setNegativeButton("Ablehnen", (d, w) -> pending.answer(false))
              .setPositiveButton("Annehmen", (d, w) -> pending.answer(true))
              .setOnCancelListener(d -> pending.answer(false))
              .show();
    }
    if (pending == null && incomingDialog != null) {
      incomingDialog.dismiss();
      incomingDialog = null;
      showingOffer = null;
    }
  }

  private final Runnable poll =
      new Runnable() {
        public void run() {
          updateState();
          handler.postDelayed(this, 800);
        }
      };

  private void toast(String s) {
    Toast.makeText(this, s, Toast.LENGTH_LONG).show();
  }

  @Override
  protected void onResume() {
    super.onResume();
    resumed = true;
    handler.post(poll);
  }

  @Override
  protected void onPause() {
    resumed = false;
    handler.removeCallbacks(poll);
    super.onPause();
  }

  @Override
  protected void onSaveInstanceState(Bundle b) {
    b.putInt("tab", tab);
    b.putString("text", selectedText);
    b.putParcelableArrayList("uris", selectedUris);
    super.onSaveInstanceState(b);
  }

  @Override
  protected void onDestroy() {
    handler.removeCallbacksAndMessages(null);
    if (incomingDialog != null) incomingDialog.dismiss();
    super.onDestroy();
  }
}
