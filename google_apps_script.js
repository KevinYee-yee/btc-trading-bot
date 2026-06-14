// ============================================================
// BTC 交易監測系統 — Google Apps Script Web App
// 貼到：試算表 → 擴充功能 → Apps Script → 全選貼上 → 儲存
// 部署：部署 → 新增部署 → 網頁應用程式 → 執行身分:我 → 存取權:任何人 → 部署
// ============================================================

const SHEET_ID = "12ADUQmL9ZqoobVN4zRzhpR_rXRk2cKs4procf1IifSU";

// doGet：提供 JSON 資料給監測網頁使用
function doGet(e) {
  const ss   = SpreadsheetApp.openById(SHEET_ID);
  const dash = ss.getSheetByName("📊 儀表板");
  const logs = ss.getSheetByName("📡 監測日誌");
  const trad = ss.getSheetByName("📋 交易紀錄");

  // 儀表板 key-value
  const dashData = {};
  if (dash && dash.getLastRow() > 0) {
    const vals = dash.getRange(1, 1, dash.getLastRow(), 2).getValues();
    vals.forEach(r => { if (r[0]) dashData[r[0]] = r[1]; });
  }

  // 最近 20 筆監測日誌
  const logRows = [];
  if (logs && logs.getLastRow() > 1) {
    const last = logs.getLastRow();
    const start = Math.max(2, last - 19);
    const data  = logs.getRange(start, 1, last - start + 1, 9).getValues();
    data.reverse().forEach(r => logRows.push(r));
  }

  // 最近 10 筆交易
  const tradeRows = [];
  if (trad && trad.getLastRow() > 1) {
    const last = trad.getLastRow();
    const start = Math.max(2, last - 9);
    const data  = trad.getRange(start, 1, last - start + 1, 7).getValues();
    data.reverse().forEach(r => tradeRows.push(r));
  }

  const output = JSON.stringify({ dashboard: dashData, logs: logRows, trades: tradeRows });
  return ContentService.createTextOutput(output).setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    const ss   = SpreadsheetApp.openById(SHEET_ID);

    if (data.type === "monitor_log") updateMonitorLog(ss, data);
    if (data.type === "trade")       updateTrade(ss, data);
    updateDashboard(ss, data);

    return ContentService.createTextOutput(JSON.stringify({ok: true}))
                         .setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({ok: false, error: err.message}))
                         .setMimeType(ContentService.MimeType.JSON);
  }
}

// ─── 儀表板 ────────────────────────────────────────────────
function updateDashboard(ss, data) {
  let sh = ss.getSheetByName("📊 儀表板") || ss.insertSheet("📊 儀表板");

  // 固定版面
  if (sh.getLastRow() === 0) {
    sh.setColumnWidth(1, 200);
    sh.setColumnWidth(2, 200);

    const headers = [
      ["🤖 BTC 策略A 自動交易監測", ""],
      ["", ""],
      ["最後更新", ""],
      ["BTC 現價", ""],
      ["布林上軌", ""],
      ["布林下軌", ""],
      ["", ""],
      ["── 帳戶狀態 ──", ""],
      ["持倉狀態", ""],
      ["現金餘額", ""],
      ["未實現損益", ""],
      ["累計報酬", ""],
      ["", ""],
      ["── 交易統計 ──", ""],
      ["總交易次數", ""],
      ["勝 / 敗", ""],
      ["勝率", ""],
      ["累計已實現損益", ""],
    ];
    sh.getRange(1, 1, headers.length, 2).setValues(headers);
    sh.getRange(1, 1).setFontSize(14).setFontWeight("bold");
    sh.getRange(8, 1).setFontWeight("bold").setBackground("#f0f0f0");
    sh.getRange(14, 1).setFontWeight("bold").setBackground("#f0f0f0");
  }

  const p     = data.portfolio || {};
  const wins  = p.wins   || 0;
  const losses= p.losses || 0;
  const total = p.total_trades || 0;
  const wr    = total > 0 ? (wins / total * 100).toFixed(1) + "%" : "—";

  const posStatus = p.position > 0
    ? `持有 ${parseFloat(p.position).toFixed(6)} BTC（進場 $${parseFloat(p.entry_price).toLocaleString()}）`
    : "空倉中";

  const unrealized = p.position > 0
    ? (parseFloat(data.price) - parseFloat(p.entry_price)) * parseFloat(p.position)
    : 0;

  const totalValue = p.position > 0
    ? parseFloat(data.price) * parseFloat(p.position)
    : parseFloat(p.capital);
  const totalReturn = ((totalValue - 1000) / 1000 * 100).toFixed(2) + "%";

  sh.getRange("B3").setValue(data.time || new Date());
  sh.getRange("B4").setValue("$" + parseFloat(data.price).toLocaleString());
  sh.getRange("B5").setValue("$" + parseFloat(data.bb_upper || 0).toLocaleString());
  sh.getRange("B6").setValue("$" + parseFloat(data.bb_lower || 0).toLocaleString());
  sh.getRange("B9").setValue(posStatus);
  sh.getRange("B10").setValue(p.position > 0 ? "—" : "$" + parseFloat(p.capital).toFixed(2));
  sh.getRange("B11").setValue(p.position > 0 ? "$" + unrealized.toFixed(2) : "—");
  sh.getRange("B12").setValue(totalReturn);
  sh.getRange("B15").setValue(total);
  sh.getRange("B16").setValue(wins + " 勝 / " + losses + " 敗");
  sh.getRange("B17").setValue(wr);
  sh.getRange("B18").setValue("$" + parseFloat(p.total_pnl || 0).toFixed(2));

  // 損益顏色
  const pnl = parseFloat(p.total_pnl || 0);
  sh.getRange("B18").setFontColor(pnl >= 0 ? "#1a7340" : "#c0392b");
  const ret = parseFloat(totalReturn);
  sh.getRange("B12").setFontColor(ret >= 0 ? "#1a7340" : "#c0392b");
}

// ─── 交易紀錄 ───────────────────────────────────────────────
function updateTrade(ss, data) {
  let sh = ss.getSheetByName("📋 交易紀錄") || ss.insertSheet("📋 交易紀錄");

  if (sh.getLastRow() === 0) {
    const h = [["時間", "動作", "BTC 價格", "數量 (BTC)", "損益 %", "帳戶餘額 (USDT)", "原因"]];
    sh.getRange(1, 1, 1, 7).setValues(h);
    sh.getRange(1, 1, 1, 7).setFontWeight("bold").setBackground("#4a4a4a").setFontColor("white");
    sh.setFrozenRows(1);
    [120, 80, 120, 120, 80, 160, 200].forEach((w, i) => sh.setColumnWidth(i+1, w));
  }

  const row = [
    data.time,
    data.action,
    parseFloat(data.price),
    parseFloat(data.qty || 0),
    data.pnl_pct || "",
    parseFloat(data.capital_after || 0),
    data.reason || "",
  ];
  sh.appendRow(row);

  const lastRow = sh.getLastRow();
  const cell    = sh.getRange(lastRow, 2);
  if (data.action === "BUY")  cell.setBackground("#d4edda").setFontColor("#1a7340");
  if (data.action === "SELL") {
    const pnl = parseFloat((data.pnl_pct || "0").replace("%",""));
    cell.setBackground(pnl >= 0 ? "#d4edda" : "#f8d7da")
        .setFontColor(pnl >= 0 ? "#1a7340" : "#c0392b");
  }
}

// ─── 監測日誌 ───────────────────────────────────────────────
function updateMonitorLog(ss, data) {
  let sh = ss.getSheetByName("📡 監測日誌") || ss.insertSheet("📡 監測日誌");

  if (sh.getLastRow() === 0) {
    const h = [["時間 (UTC)", "BTC 價格", "布林上軌", "布林下軌", "停損線", "布林下軌條件", "MACD 條件", "訊號", "帳戶狀態"]];
    sh.getRange(1, 1, 1, 9).setValues(h);
    sh.getRange(1, 1, 1, 9).setFontWeight("bold").setBackground("#4a4a4a").setFontColor("white");
    sh.setFrozenRows(1);
  }

  sh.appendRow([
    data.time,
    parseFloat(data.price),
    parseFloat(data.bb_upper || 0),
    parseFloat(data.bb_lower || 0),
    parseFloat(data.recent_low || 0),
    data.cond_bb  || "❌",
    data.cond_macd|| "❌",
    data.signal   || "無訊號",
    data.account_status || "",
  ]);

  // 有訊號那行標黃
  if (data.signal && data.signal !== "無訊號") {
    const lastRow = sh.getLastRow();
    sh.getRange(lastRow, 1, 1, 9).setBackground("#fff3cd");
  }
}
