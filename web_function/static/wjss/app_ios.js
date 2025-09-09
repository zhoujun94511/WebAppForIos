// -*- coding: utf-8 -*-
// /static/wjss/app_ios.js
// 依赖：jQuery、SweetAlert2
(function (global) {
  "use strict";

  const API = {
    refreshDevices: "/api/devices",      // GET -> { ok, devices:[] } 或 { deviceList:[] }
    getDeviceInfo: "/api/device_info",   // GET ?udid=...
    screenshot: "/api/screenshot",       // GET ?udid=... -> image blob

    // 新增的三个后端端点（已有后端实现）：
    apps: "/api/apps",                   // GET ?udid=...&list=0 -> {ok, raw}
    kill: "/api/kill",                   // POST JSON {udid, bundle_id}
    install: "/api/install",             // POST FormData {udid, file}

    // 录屏流（后端已提供）
    streamStart: "/api/screenshot/stream/start",
    streamStop: "/api/screenshot/stream/stop",

    // 启动与重启
    launch: "/api/launch",
    reboot: "/api/reboot",

    // 新增
    appsRunning: "/api/apps/running",
    crashLs: "/api/crash/ls",
    crashCp: "/api/crash/cp",
    crashRm: "/api/crash/rm",
    devmodeGet: "/api/devmode/get",
    devmodeCheck: "/api/devmode/check",  // 轻量级检测，不依赖tunnel
    devmodeEnable: "/api/devmode/enable",
    profileList: "/api/profile/list",
    profileRemove: "/api/profile/remove",
    assistive: (feature, action) => `/api/assistive/${feature}/${action}`,
    deviceEvents: "/api/devices/events",
    syslogStart: "/api/syslog/start",
    syslogStop: "/api/syslog/stop",
    batteryDetail: "/api/battery/detail",
    diskDetail: "/api/diskspace/detail",
    debugExportLogs: "/api/debug/export-logs",
    debugCleanupProcesses: "/api/debug/cleanup-processes",
  };

  // —— 小工具 ——
  let _refreshingDevices = false;
  let _allThirdPartyApps = []; // {bundleId, name?, version?}
  let _streamSession = null;   // { id, udid, url }

  function showLoading(text) {
    const el = document.getElementById("loading-overlay");
    if (el) {
      const t = el.querySelector(".loading-text");
      if (t) t.textContent = text || "Loading...";
      el.style.display = "flex";
    }
  }
  function hideLoading() {
    const el = document.getElementById("loading-overlay");
    if (el) el.style.display = "none";
  }
  async function alertAndHide(opts) {
    // 解决遮罩与提示冲突：弹窗前隐藏遮罩
    hideLoading();
    try {
      if (opts && String(opts.icon) === "success") {
        return Promise.resolve({ dismissed: true });
      }
      return Swal.fire(opts);
    } catch (e) {
      return Promise.resolve();
    }
  }

  // 通用：fetch JSON（非200抛错），以及POST JSON封装
  async function apiFetchJSON(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) {
      let msg = "HTTP " + res.status;
      try { const j = await res.json(); msg = j.msg || j.raw || msg; } catch {}
      throw new Error(msg);
    }
    return res.json();
  }
  function apiPostJSON(url, obj) {
    return apiFetchJSON(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj || {})
    });
  }

  // 统一通知封装
  function notifySuccess(title, text, timer=1200) { return alertAndHide({ icon: "success", title, text, timer }); }
  function notifyError(title, text, timer=3000) { return alertAndHide({ icon: "error", title, text, timer }); }
  function notifyInfo(title, text, timer=1800) { return alertAndHide({ icon: "info", title, text, timer }); }
  function notifyWarning(title, text, timer=2000) { return alertAndHide({ icon: "warning", title, text, timer }); }
  function setBusy($btn, busy) {
    if ($btn && $btn.length) $btn.prop("disabled", !!busy);
  }

  // 受控日志（默认静默；仅当 window.__IOSAPP_DEBUG__ 为真时输出）
  function logDebug(){ try { if (window && window.__IOSAPP_DEBUG__) console.debug.apply(console, arguments); } catch(_){} }
  function logWarn(){ try { if (window && window.__IOSAPP_DEBUG__) console.warn.apply(console, arguments); } catch(_){} }
  function getUDID() {
    return $("#device-select").val() || "";
  }
  function syncHiddenUdid() {
    $("#selected-udid").val(getUDID());
  }

  // 补充通用函数（避免未定义）
  async function withUdid($btn, fn) {
    const udid = getUDID();
    if (!udid) {
      await alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
      if ($btn && $btn.length) setBusy($btn, false);
      return null;
    }
    return fn && fn(udid);
  }
  function bindDocNS(eventName, ns, selector, handler) {
    $(document).off(`${eventName}.${ns}`).on(`${eventName}.${ns}`, selector, handler);
  }
  function bindDocGlobal(eventName, ns, handler) {
    $(document).off(`${eventName}.${ns}`).on(`${eventName}.${ns}`, handler);
  }

  function fmtModel(model) {
    if (!model) return "";
    const s = String(model).trim();
    return s.split(",")[0];
  }

  function sectionTitle(text, extraClass) {
    const cls = extraClass ? ("info-section-title " + extraClass) : "info-section-title";
    return `<div class="${cls}">${text}：</div>`;
  }

  function shouldHideBaseLabel(label) {
    const s = String(label || "").trim();
    // 兼容 Wi‑Fi 的不同连字符：普通连字符- 或非折行连字符‑
    return /(Wi[-‑]?Fi地址|蓝牙地址|以太网地址)/i.test(s);
  }

  // =========================
  // 设备列表
  // =========================
  async function refreshDevices() {
    if (_refreshingDevices) return;
    _refreshingDevices = true;

    const $btn = $("#refresh-devices-button");
    setBusy($btn, true);

    try {
      const res = await fetch(API.refreshDevices + "?details=1", { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();

      // 归一化
      const raw = Array.isArray(data) ? data : (data.devices || data.deviceList || []);
      const items = raw.map(d => {
        if (typeof d === "string") return { name: d, udid: d, model: "" };
        const udid  = d.udid || d.UDID || d.Udid;
        const model = d.model || d.ProductType || "";
        const name  = d.name  || d.ProductName || model || udid;
        return { name, udid, model };
      });

      // 去重（按 UDID）
      const seen = new Set();
      const uniqueItems = [];
      for (const it of items) {
        if (!it.udid || seen.has(it.udid)) continue;
        seen.add(it.udid);
        uniqueItems.push(it);
      }

      const $select = $("#device-select");
      const $list   = $("#ios-device-list");
      $select.empty(); $list.empty();

      if (uniqueItems.length === 0) {
        await alertAndHide({ icon: "warning", title: "未检测到设备", text: "请检查连接。", timer: 2500 });
        $select.html('<option value="">未检测到设备</option>');
        $list.append('<li class="list-group-item text-muted">未检测到设备</li>');
        $("#selected-udid").val("");
        // 没有设备时显示提示
        showDevModeStatus("no-device");
        return;
      }

      // 标签：型号 · UDID；值仍然是 UDID
      $select.html(uniqueItems.map(it => {
        const label = (it.model ? fmtModel(it.model) + " · " : "") + it.udid;
        return `<option value="${it.udid}">${label}</option>`;
      }).join(""));

      $list.html(uniqueItems.map(it => {
        const label = (it.model ? fmtModel(it.model) + " · " : "") + it.udid;
        const title = it.name || it.model || "iOS 设备";
        return `<li class="list-group-item"><b>${title}</b><br>${label}</li>`;
      }).join(""));

      $("#selected-udid").val(uniqueItems[0].udid);
      // 触发设备选择变化事件，启动开发者模式检测
      setTimeout(() => {
        $("#device-select").trigger("change");
      }, 100);
      

    } catch (e) {
      await alertAndHide({ icon: "error", title: "刷新设备失败", text: e.message || "未知错误", timer: 3000 });
    } finally {
      setBusy($btn, false);
      _refreshingDevices = false;
    }
  }

  // =========================
  // 设备信息
  // =========================
  async function getDeviceInfo() {
    const $btn = $("#get-device-info-button");
    setBusy($btn, true);

    const udid = getUDID();
    if (!udid) {
      await alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
      return setBusy($btn, false);
    }

    try {
      // 并行请求：基础信息 + 电池合并 + 磁盘
      const [resBase, resBat, resDisk] = await Promise.all([
        fetch(`${API.getDeviceInfo}?udid=${encodeURIComponent(udid)}`, { cache: "no-store" }),
        fetch(`${API.batteryDetail}?udid=${encodeURIComponent(udid)}`, { cache: "no-store" }),
        fetch(`${API.diskDetail}?udid=${encodeURIComponent(udid)}`, { cache: "no-store" }),
      ]);

      const base = await resBase.json().catch(()=>({ok:false}));
      const bat = await resBat.json().catch(()=>({ok:false}));
      const dsk = await resDisk.json().catch(()=>({ok:false}));

      const baseList = Array.isArray(base.info) ? base.info : [];
      // 过滤不展示项
      const filtered = baseList.filter(it => !shouldHideBaseLabel(it.label));
      const head = filtered.slice(0, 4);
      const tail = filtered.slice(4);
      const headHtml = head.map(it => {
        const label = String(it.label || "");
        let value = String(it.value ?? "");
        if (label === "产品型号") value = fmtModel(value);
        return `<div><span class="info-k">${label}：</span> ${value}</div>`;
      }).join("") || '<div class="text-muted">暂无可显示的设备信息</div>';
      const tailHtml = tail.map(it => {
        const label = String(it.label || "");
        let value = String(it.value ?? "");
        if (label === "产品型号") value = fmtModel(value);
        return `<div><span class="info-k">${label}：</span> ${value}</div>`;
      }).join("");

      let html = sectionTitle("常规设备信息") + headHtml;
      if (tail.length) {
        html += `
          <div class="mt-1">
            <a href="#" id="device-info-toggle" class="small">展开更多</a>
            <div id="device-info-more" style="display:none;" class="mt-2">${tailHtml}</div>
          </div>`;
      }

      // 电池（默认折叠，统一纳入更多区）
      const ck = (bat && bat.detail && bat.detail.batterycheck) || {};
      const rg = (bat && bat.detail && bat.detail.batteryregistry) || {};
      const bCap = (ck.BatteryCurrentCapacity != null) ? `${ck.BatteryCurrentCapacity}%` : "UNKNOWN";
      const temp = (rg.Temperature != null) ? (rg.Temperature/100).toFixed(2) + "°C" : "UNKNOWN";
      const volt = (rg.Voltage != null) ? (rg.Voltage/1000).toFixed(3) + " V" : "UNKNOWN";
      const design = (rg.DesignCapacity != null) ? `${rg.DesignCapacity} mAh` : "UNKNOWN";
      const nominal = (rg.NominalChargeCapacity != null) ? `${rg.NominalChargeCapacity} mAh` : "UNKNOWN";

      const batteryInner = `
        <div><span class="info-k">电池容量：</span> ${bCap}</div>
        <div><span class="info-k">电池温度：</span> ${temp}</div>
        <div><span class="info-k">电池电压：</span> ${volt}</div>
        <div><span class="info-k">设计容量：</span> ${design}</div>
        <div><span class="info-k">标称容量：</span> ${nominal}</div>
      `;

      // 磁盘（默认折叠，统一纳入更多区）
      let bs = "UNKNOWN", free = "UNKNOWN", used = "UNKNOWN", total = "UNKNOWN";
      if (dsk && dsk.ok && dsk.raw) {
        let parsed = false;
        try {
          const disk = JSON.parse(dsk.raw);
          if (disk) {
            bs = disk.BlockSize != null ? `${disk.BlockSize}KB` : "UNKNOWN";
            free = disk.FreeSpace || disk.free || "UNKNOWN";
            used = disk.UsedSpace || disk.used || "UNKNOWN";
            total = disk.TotalSpace || disk.total || "UNKNOWN";
            parsed = true;
          }
        } catch {}
        if (!parsed) {
          const text = String(dsk.raw);
          const mBlock = text.match(/BlockSize:\s*([0-9.]+)/i);
          const mFree = text.match(/FreeSpace:\s*([^\n\r]+)/i);
          const mUsed = text.match(/UsedSpace:\s*([^\n\r]+)/i);
          const mTotal = text.match(/TotalSpace:\s*([^\n\r]+)/i);
          bs = mBlock ? `${mBlock[1]}KB` : bs;
          free = mFree ? mFree[1].trim() : free;
          used = mUsed ? mUsed[1].trim() : used;
          total = mTotal ? mTotal[1].trim() : total;
        }
      }
      const diskInner = `
        <div><span class="info-k">存储块规格：</span> ${bs}</div>
        <div><span class="info-k">闲置空间：</span> ${free}</div>
        <div><span class="info-k">已使用空间：</span> ${used}</div>
        <div><span class="info-k">总体空间：</span> ${total}</div>
      `;

      // 统一更多区内容（包含：常规尾部 + 电池 + 磁盘）稍后 append 进 #device-info-more
      const extraInfoHtml = `
        <div class="info-section-title mt-2">电池信息：</div>
        <div class="mt-2">${batteryInner}</div>
        <div class="info-section-title mt-2">磁盘信息：</div>
        <div class="mt-2">${diskInner}</div>
      `;

      $("#device-info").html(html);

      // 将电池与磁盘信息附加到统一折叠区
      if (document.getElementById("device-info-more")) {
        $("#device-info-more").append(extraInfoHtml).hide();
      }

      // 移除顶部的文字/图标折叠触发器（若存在）
      if (document.getElementById("device-info-toggle")) {
        $("#device-info-toggle").remove();
      }
      // 修正展开区容器多余外边距，避免"系统版本"与"CPU 架构"之间出现空行
      (function(){
        const $more = $("#device-info-more");
        if ($more.length) {
          $more.removeClass("mt-2");
          const $wrap = $more.parent();
          try {
            if ($wrap && $wrap.length) {
              $more.appendTo("#device-info");
              $wrap.removeClass("mt-1");
              if ($wrap.children().length === 0) $wrap.remove();
            }
          } catch(_) {}
        }
      })();

      // 添加底部居中的折叠按钮（内联SVG双箭头）
      if (!document.getElementById("device-info-toggle-btn")) {
        $("#device-info").append(
          '<div class="info-collapse-btn-wrapper">' +
            '<button type="button" id="device-info-toggle-btn" class="btn btn-light border info-collapse-btn" aria-expanded="false" title="展开/收起" aria-label="展开/收起">' +
              '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" aria-hidden="true">' +
                '<polyline points="6 7 12 13 18 7"></polyline>' +
                '<polyline points="6 13 12 19 18 13"></polyline>' +
              '</svg>' +
            '</button>' +
          '</div>'
        );
      }

      // 绑定按钮点击，平滑展开/收起，并保持按钮始终在底部
      $("#device-info-toggle-btn").off("click").on("click", function(e){
        e.preventDefault();
        const $more = $("#device-info-more");
        const isOpen = $more.is(":visible");
        if (isOpen) {
          $more.slideUp(200);
        } else {
          $more.slideDown(200, function(){
            // 展开后将按钮保持在容器底部
            const $btnWrap = $("#device-info-toggle-btn").closest('.info-collapse-btn-wrapper');
            if ($btnWrap.length) {
              $btnWrap.appendTo('#device-info');
            }
          });
        }
        $(this).attr('aria-expanded', String(!isOpen));
      });

      await alertAndHide({ icon: "success", title: "获取设备信息成功", timer: 1200 });
    } catch (e) {
      await alertAndHide({ icon: "error", title: "获取设备信息出错", text: e.message, timer: 3000 });
    } finally {
      setBusy($btn, false);
    }
  }

  function renderInfoList(list) {
    const arr = Array.isArray(list) ? list : [];
    const html = arr.filter(it => !shouldHideBaseLabel(it.label)).map(it => {
      const label = String(it.label || "");
      let value = String(it.value ?? "");
      if (label === "产品型号") value = fmtModel(value);
      return `<div><span class="info-k">${label}：</span> ${value}</div>`;
    }).join("")
              || '<div class="text-muted">暂无可显示的设备信息</div>';
    $("#device-info").html(html);
  }

  async function renderBatteryAndDisk(part = "both") {
    const udid = getUDID();
    if (!udid) return;
    try {
      const needBattery = part === "both" || part === "battery";
      const needDisk = part === "both" || part === "disk";
      const tasks = [];
      if (needBattery) tasks.push(fetch(`${API.batteryDetail}?udid=${encodeURIComponent(udid)}`)); else tasks.push(Promise.resolve(null));
      if (needDisk) tasks.push(fetch(`${API.diskDetail}?udid=${encodeURIComponent(udid)}`)); else tasks.push(Promise.resolve(null));
      const [bRes, dRes] = await Promise.all(tasks);
      const bData = bRes ? await bRes.json() : null;
      const dData = dRes ? await dRes.json() : null;

      const root = $("#device-info");
      let html = root.html() || "";

      // 去除旧块（简单基于标题关键字）
      if (needBattery) html = html.replace(/<div class=\"info-section-title mt-2\">电池信息：<\/div>[\s\S]*?(?=(<div class=\"info-section-title mt-2\">|$))/g, "");
      if (needDisk) html = html.replace(/<div class=\"info-section-title mt-2\">磁盘信息：<\/div>[\s\S]*?(?=(<div class=\"info-section-title mt-2\">|$))/g, "");

      const parts = [];
      if (needBattery && bData && bData.ok) {
        const ck = bData.detail && bData.detail.batterycheck || {};
        const rg = bData.detail && bData.detail.batteryregistry || {};
        if (ck || rg) {
          parts.push("<div class=\"info-section-title mt-2\">电池信息：</div>");
          if (ck) {
            parts.push(`<div><span class=\"info-k\">电池容量：</span> ${ck.BatteryCurrentCapacity ?? "?"}%</div>`);
          }
          if (rg) {
            const temp = rg.Temperature != null ? (rg.Temperature/100).toFixed(2) + "°C" : "?";
            const volt = rg.Voltage != null ? (rg.Voltage/1000).toFixed(3) + " V" : "?";
            parts.push(`<div><span class=\"info-k\">电池温度：</span> ${temp}</div>`);
            parts.push(`<div><span class=\"info-k\">电池电压：</span> ${volt}</div>`);
            parts.push(`<div><span class=\"info-k\">设计容量：</span> ${rg.DesignCapacity ?? "?"} mAh</div>`);
            parts.push(`<div><span class=\"info-k\">标称容量：</span> ${rg.NominalChargeCapacity ?? "?"} mAh</div>`);
          }
        }
      }
      if (needDisk && dData && dData.ok && dData.raw) {
        const lines = [];
        let parsed = false;
        try {
          const disk = JSON.parse(dData.raw);
          const bs = disk.BlockSize ? `${disk.BlockSize}KB` : undefined;
          const free = disk.FreeSpace || disk.free || undefined;
          const used = disk.UsedSpace || disk.used || undefined;
          const total = disk.TotalSpace || disk.total || undefined;
          if (bs) lines.push(`<div>存储块规格：${bs}</div>`);
          if (free) lines.push(`<div>闲置空间：${free}</div>`);
          if (used) lines.push(`<div>已使用空间：${used}</div>`);
          if (total) lines.push(`<div>总体空间：${total}</div>`);
          parsed = true;
        } catch {}
        if (!parsed) {
          const text = String(dData.raw);
          const mBlock = text.match(/BlockSize:\s*([0-9.]+)/i);
          const mFree = text.match(/FreeSpace:\s*([^\n\r]+)/i);
          const mUsed = text.match(/UsedSpace:\s*([^\n\r]+)/i);
          const mTotal = text.match(/TotalSpace:\s*([^\n\r]+)/i);
          const bs = mBlock ? `${mBlock[1]}KB` : undefined;
          const free = mFree ? mFree[1].trim() : undefined;
          const used = mUsed ? mUsed[1].trim() : undefined;
          const total = mTotal ? mTotal[1].trim() : undefined;
          if (bs) lines.push(`<div>存储块规格：${bs}</div>`);
          if (free) lines.push(`<div>闲置空间：${free}</div>`);
          if (used) lines.push(`<div>已使用空间：${used}</div>`);
          if (total) lines.push(`<div>总体空间：${total}</div>`);
        }
        if (lines.length) {
          parts.push("<div class=\"info-section-title mt-2\">磁盘信息：</div>");
          parts.push(lines.join(""));
        }
      }

      root.html(html + parts.join(""));
    } catch {}
  }


  // =========================
  // 获取三方应用 & 搜索 & 停止
  // =========================

  // 解析 /api/apps 的 raw 输出为统一数组
  function parseAppsRaw(raw) {
    const apps = [];
    if (!raw) return apps;

    // 优先 JSON
    try {
      const data = JSON.parse(raw);
      const arr = Array.isArray(data) ? data
                : (data.apps || data.Apps || data.applications || data) || [];
      for (const obj of arr) {
        if (!obj || typeof obj !== "object") continue;
        const bundleId = obj.CFBundleIdentifier || obj.bundleID || obj.bundleId || obj.Bundle || obj.id;
        const name     = obj.CFBundleDisplayName || obj.CFBundleName || obj.BundleName || obj.name || obj.Name || "";
        const version  = obj.CFBundleShortVersionString || obj.ShortVersion || obj.version || obj.Version || "";
        const appType  = obj.ApplicationType || obj.appType || obj.Type || "";

        // 过滤系统应用：优先看 ApplicationType，其次看包名前缀
        const isSystem = /system/i.test(String(appType)) || (bundleId && bundleId.startsWith("com.apple."));
        if (!bundleId || isSystem) continue;

        apps.push({ bundleId, name, version });
      }
    } catch {
      // 纯文本回退：尝试  "bundleId - Name - Version" 的格式
      const lines = String(raw).split(/\r?\n/);
      for (const line of lines) {
        const m = line.match(/^\s*([a-zA-Z0-9._\-]+)\s*(?:-\s*([^-\n]+))?(?:-\s*([^\n]+))?\s*$/);
        if (!m) continue;
        const bundleId = (m[1] || "").trim();
        if (!bundleId || bundleId.startsWith("com.apple.")) continue;
        const name    = (m[2] || "").trim();
        const version = (m[3] || "").trim();
        apps.push({ bundleId, name, version });
      }
    }

    // 去重
    const seen = new Set();
    return apps.filter(a => {
      if (!a.bundleId || seen.has(a.bundleId)) return false;
      seen.add(a.bundleId);
      return true;
    });
  }

  async function getInstalledApps() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });

    const $btn = $("#get-installed-apps-button");
    setBusy($btn, true);

    try {
      // 取完整 JSON（list=0），便于区分系统/三方
      const url = `${API.apps}?udid=${encodeURIComponent(udid)}&list=0`;
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();

      if (!data.ok) throw new Error(data.raw || "获取应用列表失败");
      const apps = parseAppsRaw(data.raw);

      _allThirdPartyApps = apps;
      renderAppsList(_allThirdPartyApps);

      if (apps.length === 0) {
        await alertAndHide({ icon: "info", title: "三方应用为空", text: "未检索到三方应用。", timer: 2000 });
      } else {
        await alertAndHide({ icon: "success", title: "获取成功", text: `共 ${apps.length} 个三方应用`, timer: 1200 });
      }
    } catch (e) {
      await alertAndHide({ icon: "error", title: "获取失败", text: e.message || "未知错误", timer: 3000 });
    } finally {
      setBusy($btn, false);
    }
  }

    function renderAppsList(apps) {
      const $sel = $("#app-select");
      $sel.empty();

      if (!apps || apps.length === 0) {
        $sel.append('<option value="">（三方应用为空）</option>');
        return;
      }

      const options = apps
        .slice()
        .sort((a, b) => (a.name || a.bundleId).localeCompare(b.name || b.bundleId))
        .map(a => {
          const label = (a.name ? a.name + " · " : "") + a.bundleId + (a.version ? ` · v${a.version}` : "");
          return `<option value="${a.bundleId}">${label}</option>`;
        })
        .join("");
      $sel.html(options);
    }


  // 输入框过滤
  function filterAppsBySearch() {
    const q = ($("#app-search-input").val() || "").trim().toLowerCase();
    const filtered = !q
      ? _allThirdPartyApps
      : _allThirdPartyApps.filter(a =>
          (a.bundleId || "").toLowerCase().includes(q) ||
          (a.name || "").toLowerCase().includes(q)
        );
    renderAppsList(filtered);
  }

  // Kill 停止应用
    async function stopApp() {
      const udid = getUDID();
      if (!udid) {
        return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
      }

      const bundleId = $("#app-select").val();
      if (!bundleId) {
        return alertAndHide({ icon: "warning", title: "请选择要停止的应用", timer: 2000 });
      }

      const $btn = $("#stop-app-button");
      setBusy($btn, true);

      try {
        const res = await fetch(API.kill, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ udid, bundle_id: bundleId })
        });
        if (!res.ok) throw new Error("HTTP " + res.status);

        const data = await res.json();
        if (!data.ok) {
          const msg = data.msg || data.raw || "停止失败";

          // 针对"未运行"提示单独处理
          if (/未运行/.test(msg)) {
            await alertAndHide({
              icon: "warning",
              title: "应用未运行",
              text: msg,
              timer: 2500
            });
          } else {
            await alertAndHide({
              icon: "error",
              title: "停止失败",
              text: msg,
              timer: 3000
            });
          }
          return; // 提前结束
        }

        // 正常 Kill 成功
        await alertAndHide({
          icon: "success",
          title: "已停止",
          text: bundleId,
          timer: 1200
        });
      } catch (e) {
        await alertAndHide({
          icon: "error",
          title: "停止失败",
          text: e.message || "未知错误",
          timer: 3000
        });
      } finally {
        setBusy($btn, false);
      }
    }


  // =========================
  // IPA 安装（单文件、格式校验）
  // =========================
  function validateIpaInput() {
    const input = document.getElementById("ipa-file");
    if (!input) return { ok: false, msg: "未找到文件选择器" };

    const files = input.files;
    if (!files || files.length === 0) return { ok: false, msg: "请选择 .ipa 文件" };
    if (files.length > 1) return { ok: false, msg: "仅支持单个 .ipa 文件" };

    const file = files[0];
    const name = (file.name || "").toLowerCase();
    if (!name.endsWith(".ipa")) return { ok: false, msg: "仅支持 .ipa 格式" };

    return { ok: true, file };
  }

  async function installIpa() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });

    const check = validateIpaInput();
    if (!check.ok) return alertAndHide({ icon: "warning", title: "文件校验失败", text: check.msg, timer: 2500 });

    const file = check.file;
    const $btn = $("#install-ipa-button");
    setBusy($btn, true);

    try {
      const fd = new FormData();
      fd.append("udid", udid);
      fd.append("file", file);

      const res = await fetch(API.install, { method: "POST", body: fd });
      if (!res.ok) {
        let msg = "HTTP " + res.status;
        try { const e = await res.json(); msg = e.msg || msg; } catch {}
        throw new Error(msg);
      }

      const data = await res.json();
      if (!data.ok) throw new Error(data.raw || "安装失败");

      $("#install-result-tip").text(`安装任务已提交：${file.name}`);
      await alertAndHide({ icon: "success", title: "安装成功", text: file.name, timer: 1500 });

      // 清空选择
      $("#ipa-file").val("");
    } catch (e) {
      await alertAndHide({ icon: "error", title: "安装失败", text: e.message || "未知错误", timer: 3000 });
    } finally {
      setBusy($btn, false);
    }
  }

  // =========================
  // 截图
  // =========================
  async function takeScreenshot() {
    const $btn = $("#take-screenshot-button");
    setBusy($btn, true);

    const udid = getUDID();
    if (!udid) {
      await alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
      return setBusy($btn, false);
    }

    showLoading("正在截屏...");
    try {
      // 后端将以附件形式返回，并带上规范化文件名
      const res = await fetch(`${API.screenshot}?udid=${encodeURIComponent(udid)}`, { cache: "no-store" });
      if (!res.ok) {
        try { const err = await res.json(); throw new Error(err.msg || `HTTP ${res.status}`); } catch {
          throw new Error(`HTTP ${res.status}`);
        }
      }

      // 从响应头解析文件名
      const cd = res.headers.get("Content-Disposition") || "";
      let downloadName = "screenshot.png";
      try {
        const m = cd.match(/filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i);
        if (m) {
          downloadName = decodeURIComponent(m[1] || m[2] || downloadName);
        }
      } catch {}

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);

      $("#screenshot-preview").html(
        `<div class="d-flex flex-column h-100" style="padding: 20px;">
           <img src="${url}" class="img-fluid rounded border flex-grow-1" style="object-fit: contain; max-height: calc(100% - 60px);"/>
           <div class="mt-2 text-center">
             <a href="${url}" download="${downloadName}" class="btn btn-sm btn-outline-secondary">下载图片</a>
           </div>
         </div>`
      );

      await alertAndHide({ icon: "success", title: "截屏完成", timer: 1200 });
    } catch (e) {
      await alertAndHide({ icon: "error", title: "截屏失败", text: e.message, timer: 3000 });
    } finally {
      setBusy($btn, false);
    }
  }

  // =========================
  // 录屏（基于 screenshot --stream）
  // =========================
  async function startScreenStream() {
    const udid = getUDID();
    if (!udid) {
      return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    }

    const $btn = $("#record-screen-button");
    setBusy($btn, true);
    showLoading("正在准备录屏...");

    try {
      const res = await fetch(API.streamStart, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ udid })
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (!data.ok) throw new Error(data.msg || "启动录屏失败");

      _streamSession = { id: data.id, udid, url: data.url };

      // 使用 <img> 播放 MJPEG 流
      $("#screenshot-preview").html(
        `<div class="d-flex flex-column h-100" style="padding: 20px;">
           <img id="screen-stream" src="${data.url}" class="img-fluid rounded border flex-grow-1" style="object-fit: contain; max-height: calc(100% - 60px);"/>
           <div id="stream-status" class="mt-2 small text-muted text-center">录屏进行中（MJPEG 流）</div>
         </div>`
      );

      // 添加图片加载错误处理
      const streamImg = document.getElementById("screen-stream");
      let retryCount = 0;
      const maxRetries = 3;

      streamImg.addEventListener("error", function() {
        retryCount++;
        logWarn(`MJPEG 流加载失败 (尝试 ${retryCount}/${maxRetries})`);

        if (retryCount <= maxRetries) {
          $("#stream-status").text(`连接中断，尝试重连 (${retryCount}/${maxRetries})...`);

          // 延迟重试
          setTimeout(() => {
            streamImg.src = data.url + "?t=" + Date.now(); // 添加时间戳避免缓存
          }, 2000 * retryCount); // 递增延迟
        } else {
          $("#stream-status").html(`<span class="text-danger">流连接失败，请重新开始录屏</span>`);
          // 自动停止录屏
          setTimeout(stopScreenStream, 1000);
        }
      });

      streamImg.addEventListener("load", function() {
        retryCount = 0; // 重置重试计数
        $("#stream-status").text("录屏进行中（MJPEG 流）");
      });

      $btn.text("停止录屏");
      await alertAndHide({ icon: "success", title: "录屏已开始", timer: 900 });
    } catch (e) {
      await alertAndHide({ icon: "error", title: "启动录屏失败", text: e.message || "未知错误", timer: 3000 });
    } finally {
      hideLoading();
      setBusy($btn, false);
    }
  }

  async function stopScreenStream() {
    if (!_streamSession) {
      logWarn("stopScreenStream: _streamSession 为空");
      return;
    }
    const { id, udid } = _streamSession;

    logDebug("stopScreenStream: _streamSession =", _streamSession);
    logDebug("stopScreenStream: 发送数据 =", { udid, id });

    const $btn = $("#record-screen-button");
    setBusy($btn, true);

    try {
      const res = await fetch(API.streamStop, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ udid, id })
      });
      if (!res.ok) {
        const errorText = await res.text();
        console.error("停止录屏HTTP错误:", res.status, errorText);
        throw new Error("HTTP " + res.status);
      }
      const data = await res.json();
      if (!data.ok) throw new Error(data.msg || "停止录屏失败");

      _streamSession = null;

      const dl = data.download_url ? `<a href="${data.download_url}" class="btn btn-sm btn-success" download>下载录屏文件</a>` : "";
      $("#screenshot-preview").html(
        dl ? `<div class="d-flex flex-column h-100 justify-content-center align-items-center" style="padding: 20px;">${dl}</div>` : ""
      );
      $btn.text("设备录屏");
      await alertAndHide({ icon: "success", title: "录屏已停止", timer: 900 });
    } catch (e) {
      console.error("stopScreenStream 错误:", e);
      await alertAndHide({ icon: "error", title: "停止录屏失败", text: e.message || "未知错误", timer: 3000 });
    } finally {
      setBusy($btn, false);
    }
  }

  async function toggleScreenRecord() {
    if (_streamSession) return stopScreenStream();
    return startScreenStream();
  }

  // 启动应用
  async function launchApp() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });

    const bundleId = $("#app-select").val();
    if (!bundleId) return alertAndHide({ icon: "warning", title: "请选择要启动的应用", timer: 2000 });

    const $btn = $("#launch-app-button");
    setBusy($btn, true);
    try {
      const res = await fetch(API.launch, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ udid, bundle_id: bundleId, wait: false })
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (!data.ok) throw new Error(data.msg || data.raw || "启动失败");
      await alertAndHide({ icon: "success", title: "已启动", text: bundleId, timer: 1200 });
    } catch (e) {
      await alertAndHide({ icon: "error", title: "启动失败", text: e.message || "未知错误", timer: 3000 });
    } finally {
      setBusy($btn, false);
    }
  }

  // 重启设备
  async function rebootDevice() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });

    const $btn = $("#reboot-device-button");
    setBusy($btn, true);
    try {
      const ok = await Swal.fire({
        icon: "warning",
        title: "确认重启设备？",
        showCancelButton: true,
        confirmButtonText: "确认",
        cancelButtonText: "取消",
      }).then(r => r.isConfirmed);
      if (!ok) return;

      const res = await fetch(API.reboot, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ udid })
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (!data.ok) throw new Error(data.msg || data.raw || "重启失败");
      await alertAndHide({ icon: "success", title: "重启执行中", timer: 1200 });
    } catch (e) {
      await alertAndHide({ icon: "error", title: "重启失败", text: e.message || "未知错误", timer: 3000 });
    } finally {
      setBusy($btn, false);
    }
  }

  async function rebootDeviceFromDM() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const ok = await Swal.fire({ icon: "warning", title: "确认重启设备？", showCancelButton: true }).then(r => r.isConfirmed);
    if (!ok) return;
    const res = await fetch(API.reboot, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ udid }) });
    const data = await res.json();
    if (!data.ok) return alertAndHide({ icon: "error", title: "重启失败", text: data.msg || data.raw || "" });
    return alertAndHide({ icon: "success", title: "重启执行中", timer: 1200 });
  }

  async function listCrashes() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const pattern = (document.getElementById("crash-search-input").value || "").trim();
    const url = `${API.crashLs}?udid=${encodeURIComponent(udid)}${pattern ? `&pattern=${encodeURIComponent(pattern)}` : ""}`;
    try {
      const res = await fetch(url, { cache: "no-store" });
      const data = await res.json();
      if (!data.ok) throw new Error(data.msg || data.raw || "获取失败");
      const items = Array.isArray(data.items) ? data.items : [];
      // 自定义下拉多选菜单
      const $menu = $("#crash-multi-menu").empty();
      $menu.append(`<div class="menu-toolbar d-flex justify-content-between align-items-center"><div class="form-check mb-0"><input class="form-check-input" type="checkbox" id="crash-multi-all"><label class="form-check-label" for="crash-multi-all">全选</label></div><div class="d-flex gap-3"><a href="#" id="crash-multi-clear">清空</a><a href="#" id="crash-multi-close">✕</a></div></div>`);
      items.forEach((it, idx) => {
        if (typeof it !== 'string') return;
        const id = `crash-cb-${idx}`;
        const $row = $("<div>").addClass("form-check");
        const $input = $("<input>").addClass("form-check-input crash-cb").attr({ type: "checkbox", id, value: it });
        const $label = $("<label>").addClass("form-check-label").attr("for", id).text(it);
        $row.append($input, $label);
        $menu.append($row);
      });
      // 追加局部绑定，确保清空点击必达
      $menu.off("click.clearLocal").on("click.clearLocal", "#crash-multi-clear", function(e){
        e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        $("#crash-multi-menu .crash-cb").prop("checked", false);
        $("#crash-multi-all").prop("checked", false);
        $("#crash-select").val([]);
        updateCrashTriggerText();
        $("#crash-multi-menu").hide();
      });
      const $trigger = $("#crash-multi-trigger");
      if (items.length === 0) {
        $menu.append(`<div class="text-muted">无结果</div>`);
        $trigger.prop("disabled", true).text("无结果（请更换关键字）");
      } else {
        $trigger.prop("disabled", false).text("点击选择日志👇（支持多选）");
      }
      updateCrashTriggerText();
      if (items.length === 0) {
        await alertAndHide({ icon: "info", title: "无匹配结果", text: "请更换关键字或清空后重试", timer: 2000 });
      }
    } catch (e) {
      await alertAndHide({ icon: "error", title: "获取失败", text: e.message || "" });
    }
  }

  function _getSelected($sel) {
    // 自定义下拉多选所选
    if ($("#profile-multi").length) {
      const arrP = [];
      $("#profile-multi-menu .profile-cb:checked").each(function(){ arrP.push(this.value); });
      if (arrP.length) return arrP;
    }
    if ($("#crash-multi").length) {
      const arr = [];
      $("#crash-multi-menu .crash-cb:checked").each(function(){ arr.push(this.value); });
      if (arr.length) return arr;
    }
    // 兜底：下拉框值
    const v = $sel.val();
    if (v === null || v === undefined || v === "") return [];
    if (Array.isArray(v)) return v;
    return [v];
  }

  async function exportCrashes() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const hasList = $("#crash-multi-menu .crash-cb").length > 0;
    if (!hasList) return alertAndHide({ icon: "info", title: "请先获取Crash信息列表", timer: 1800 });
    const sels = _getSelected($("#crash-select"));
    if (sels.length === 0) {
      const okAll = await Swal.fire({ icon: "question", title: "未选择，导出全部？", showCancelButton: true }).then(r => r.isConfirmed);
      if (!okAll) return;
    }
    const body = { udid };
    if (sels.length > 1) body.patterns = sels; else if (sels.length === 1) body.pattern = sels[0]; else body.pattern = "*";
    const res = await fetch(API.crashCp, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    const data = await res.json();
    if (!data.ok) return alertAndHide({ icon: "error", title: "导出失败", text: data.raw || "" });
    // 自动下载：单/多（2）/打包（3+）
    if (data.download_url) {
      const a = document.createElement("a");
      a.href = data.download_url;
      a.download = "";
      document.body.appendChild(a); a.click(); a.remove();
      await alertAndHide({ icon: "success", title: "已开始下载", timer: 1000 });
    } else {
      await alertAndHide({ icon: "info", title: "无可下载文件", timer: 1200 });
    }
  }

  async function deleteCrashes() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const hasList = $("#crash-multi-menu .crash-cb").length > 0;
    if (!hasList) return alertAndHide({ icon: "info", title: "请先获取Crash信息列表", timer: 1800 });
    const sels = _getSelected($("#crash-select"));
    if (sels.length === 0) {
      const okAll = await Swal.fire({ icon: "warning", title: "未选择，删除全部？", text: "将删除所有匹配(*)", showCancelButton: true }).then(r => r.isConfirmed);
      if (!okAll) return;
    }
    const ok = await Swal.fire({ icon: "warning", title: "确认删除所选？", showCancelButton: true }).then(r => r.isConfirmed);
    if (!ok) return;
    const body = { udid };
    if (sels.length > 1) body.patterns = sels; else if (sels.length === 1) body.pattern = sels[0]; else body.pattern = "*";
    const res = await fetch(API.crashRm, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    const data = await res.json();
    if (!data.ok) return alertAndHide({ icon: "error", title: "删除失败", text: data.raw || "" });
    await alertAndHide({ icon: "success", title: "已删除", timer: 1200 });
    listCrashes();
  }

  async function profileList() {
    await withUdid(null, async (udid) => {
      const data = await apiFetchJSON(`${API.profileList}?udid=${encodeURIComponent(udid)}`);
      if (!data.ok) return notifyError("获取失败", data.raw || "");
      const items = Array.isArray(data.items) ? data.items : [];

      // 归一化：支持字符串或对象（优先显示 Metadata.PayloadDisplayName / Manifest.Description）
      const norm = items.map((it) => {
        if (typeof it === "string") return { value: it, label: it };
        try {
          const name = (it && it.Metadata && (it.Metadata.PayloadDisplayName || it.Metadata.PayloadName))
                    || (it && it.Manifest && it.Manifest.Description)
                    || (it && it.Identifier)
                    || String(it);
          // 仅显示名称；不再拼接短ID
          return { value: String(name), label: String(name) };
        } catch {
          const s = String(it);
          return { value: s, label: s };
        }
      });

      // 隐藏下拉兜底（用于 _getSelected 兼容）
      const $sel = $("#profile-select").empty();
      norm.forEach(it => $sel.append(`<option value="${it.value}">${it.label}</option>`));

      // 渲染自定义下拉多选
      const $menu = $("#profile-multi-menu").empty();
      $menu.append(`<div class="menu-toolbar d-flex justify-content-between align-items-center"><div class="form-check mb-0"><input class="form-check-input" type="checkbox" id="profile-multi-all"><label class="form-check-label" for="profile-multi-all">全选</label></div><div class="d-flex gap-3"><a href="#" id="profile-multi-clear">清空</a><a href="#" id="profile-multi-close">✕</a></div></div>`);
      norm.forEach((it, idx) => {
        const id = `profile-cb-${idx}`;
        const $row = $("<div>").addClass("form-check");
        const $input = $("<input>").addClass("form-check-input profile-cb").attr({ type: "checkbox", id, value: it.value });
        const $label = $("<label>").addClass("form-check-label").attr("for", id).text(it.label);
        $row.append($input, $label);
        $menu.append($row);
      });
      const $trigger = $("#profile-multi-trigger");
      if (norm.length === 0) {
        $menu.append(`<div class="text-muted">无结果</div>`);
        $trigger.prop("disabled", true).text("无结果（请先获取）");
      } else {
        $trigger.prop("disabled", false).text("点击选择配置👇（支持多选）");
      }
      updateProfileTriggerText();
    });
  }

  async function profileRemove() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const sels = _getSelected($("#profile-select"));
    if (sels.length === 0) return alertAndHide({ icon: "warning", title: "请选择要删除的配置文件", timer: 2000 });
    const ok = await Swal.fire({ icon: "warning", title: "确认移除所选配置文件？", showCancelButton: true }).then(r => r.isConfirmed);
    if (!ok) return;
    const res = await fetch(API.profileRemove, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ udid, names: sels }) });
    const data = await res.json();
    if (!data.ok) return alertAndHide({ icon: "error", title: "移除失败", text: (data.results||[]).map(x=>`${x.name}:${x.ok?'ok':'fail'}`).join("\n") });
    await alertAndHide({ icon: "success", title: "已移除", timer: 1200 });
    profileList();
  }

  async function assistiveAction(action) {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const feature = $("#assistive-feature-select").val();
    const res = await fetch(API.assistive(feature, action), { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ udid }) });
    const data = await res.json();
    if (!data.ok) return alertAndHide({ icon: "error", title: "执行失败", text: data.raw || "" });
    await alertAndHide({ icon: "success", title: "已执行", timer: 900 });
  }

  async function appsRunning() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const res = await fetch(`${API.appsRunning}?udid=${encodeURIComponent(udid)}`);
    const data = await res.json();
    if (!data.ok) return $("#apps-running-list").text("获取失败");
    const list = Array.isArray(data.list) ? data.list : [];
    const html = list.length ? list.map(p => `<div>${p.Name || "(unknown)"} · PID ${p.Pid || "?"}</div>`).join("") : '<div class="text-muted">无运行中应用</div>';
    $("#apps-running-list").html(html);
  }

  let _syslogSession = null;
  let _syslogES = null;
  let _syslogBuf = [];
  let _syslogFlushScheduled = false;
  const _syslogMaxLines = 1000;    // 最大保留行数
  const _syslogBatchSize = 300;    // 每帧最多渲染条数
  let _syslogGen = 0;              // 清空/重连时自增，丢弃旧批次

  function _escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(ch){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[ch]);
    });
  }
  function _buildHighlighter(){
    const kwRaw = (document.getElementById('syslog-kw')?.value || '').trim();
    if (!kwRaw) return null;
    const parts = kwRaw.split(',').map(s=>s.trim()).filter(Boolean).map(s=>s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    if (!parts.length) return null;
    const re = new RegExp('(' + parts.join('|') + ')', 'gi');
    return function(text){
      const safe = _escapeHtml(text);
      return safe.replace(re, '<span class="hl">$1</span>');
    };
  }

  function _syslogScheduleFlush() {
    if (_syslogFlushScheduled) return;
    _syslogFlushScheduled = true;
    requestAnimationFrame(_syslogFlush);
  }

  function _syslogFlush() {
    _syslogFlushScheduled = false;
    const curGen = _syslogGen; // 捕获当前代次
    const $out = $("#syslog-live");
    if (!$out.length) { _syslogBuf.length = 0; return; }
    const el = $out[0];
    const nearBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;

    const frag = document.createDocumentFragment();
    const highlighter = _buildHighlighter();
    let n = Math.min(_syslogBuf.length, _syslogBatchSize);
    for (let i = 0; i < n; i++) {
      if (curGen !== _syslogGen) return; // 清空/重连后放弃本批
      const line = _syslogBuf[i];
      const div = document.createElement('div');
      if (highlighter) {
        div.innerHTML = highlighter(line);
      } else {
        div.textContent = line;
      }
      frag.appendChild(div);
    }
    _syslogBuf.splice(0, n);
    if (curGen !== _syslogGen) return; // 再次检查
    el.appendChild(frag);

    // 仅当本次确实写入了日志时才隐藏占位文字
    if (n > 0) { $("#syslog-placeholder").hide(); }

    // 限制最大行数，超出从顶部移除
    while (el.childNodes.length > _syslogMaxLines) {
      el.removeChild(el.firstChild);
    }

    if (nearBottom) {
      el.scrollTop = el.scrollHeight;
    }

    if (_syslogBuf.length > 0) {
      _syslogScheduleFlush();
    }
  }
  async function syslogStart() {
    const udid = getUDID();
    if (!udid) return alertAndHide({ icon: "warning", title: "请选择设备", timer: 2000 });
    const res = await fetch(API.syslogStart, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ udid, parse: false }) });
    const data = await res.json();
    if (!data.ok) return alertAndHide({ icon: "error", title: "启动失败", text: data.msg || data.raw || "" });
    _syslogSession = { id: data.id, udid };
    $("#syslog-download-link").text(`日志文件：${data.file}`);

    // 打开 SSE 流
    try {
      if (_syslogES) { _syslogES.close(); _syslogES = null; }
      const url = _buildSyslogStreamUrl(udid);
      _syslogES = new EventSource(url);
      const $out = $("#syslog-live");
      if ($out.length) {
        // 开始前仅清除历史内容，保留占位符，待收到首条日志再隐藏
        $out.find('div:not(#syslog-placeholder)').remove();
      }
      _syslogBuf.length = 0; _syslogGen++;
      if (_syslogES) {
        _syslogES.onmessage = function(ev){
          const g = _syslogGen; // 捕获代次，避免旧事件在清空后生效
          _syslogBuf.push(ev.data || "");
          if (g === _syslogGen) _syslogScheduleFlush();
        };
        _syslogES.onerror = function(){ /* 静默 */ };
      }
    } catch(_) {}
    await alertAndHide({ icon: "success", title: "系统日志已开始", timer: 900 });
  }
  async function syslogStop() {
    if (!_syslogSession) return alertAndHide({ icon: "warning", title: "未开始系统日志", timer: 1500 });
    const { id, udid } = _syslogSession;
    const res = await fetch(API.syslogStop, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ udid, id }) });
    const data = await res.json();
    if (!data.ok) return alertAndHide({ icon: "error", title: "停止失败", text: data.msg || data.raw || "" });
    _syslogSession = null;
    if (_syslogES) { try { _syslogES.close(); } catch(_) {} _syslogES = null; }
    _syslogBuf.length = 0; _syslogGen++;
    
    // 停止后显示占位文字
    const $out = $('#syslog-live');
    if ($out.length) {
      // 只清空日志内容，保留占位符
      $out.find('div:not(#syslog-placeholder)').remove();
      $("#syslog-placeholder").show();
    }
    
    if (data.download_url) $("#syslog-download-link").html(`<a href="${data.download_url}" class="link-primary">下载日志</a>`);
    await alertAndHide({ icon: "success", title: "系统日志已停止", timer: 900 });
  }

  function _buildSyslogStreamUrl(udid){
    const kw = (document.getElementById('syslog-kw')?.value || '').trim();
    const lv = (document.getElementById('syslog-lv')?.value || '').trim();
    const params = new URLSearchParams();
    params.set('udid', udid);
    params.set('parse', '0');
    if (kw) params.set('kw', kw);
    if (lv) params.set('lv', lv);
    return `/api/syslog/stream?${params.toString()}`;
  }

  function bindDeviceEventsSSE() {
    try {
      const es = new EventSource(API.deviceEvents);
      es.onmessage = function(ev){
        // 简单策略：接到任何事件都刷新设备列表
        refreshDevices();
      };
      es.onerror = function(){ /* 静默 */ };
    } catch(e) {
      // 忽略 SSE 不可用
    }
  }

  function bindDMEvents() {
    $("#dm-reboot-button").off("click").on("click", function(e){ e.preventDefault(); rebootDeviceFromDM(); });
    $("#crash-list-button").off("click").on("click", function(e){ e.preventDefault(); listCrashes(); });
    $("#crash-export-button").off("click").on("click", function(e){ e.preventDefault(); exportCrashes(); });
    $("#crash-delete-button").off("click").on("click", function(e){
      e.preventDefault();
      deleteCrashes();
    });
    $("#crash-multi-trigger").off("click").on("click", function(e){
      e.preventDefault(); e.stopPropagation();
             const $m = $("#crash-multi-menu");
       $m.toggle();
       return false;
    });
    $(document).off("click.crashmenu").on("click.crashmenu", function(){
      $("#crash-multi-menu").hide();
    });
    $("#crash-multi-menu").off("click").on("click", function(e){ e.stopPropagation(); });
    $(document).off("change.crashmenu").on("change.crashmenu", "#crash-multi-all", function(){
      const on = this.checked;
      $("#crash-multi-menu .crash-cb").prop("checked", on);
      updateCrashTriggerText();
    });
    $(document).off("click.crashclear").on("click.crashclear", "#crash-multi-clear", function(e){
      e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      $("#crash-multi-menu .crash-cb").prop("checked", false);
      $("#crash-multi-all").prop("checked", false);
      $("#crash-select").val([]);
      updateCrashTriggerText();
    });
    $(document).off("change.crashcb").on("change.crashcb", "#crash-multi-menu .crash-cb", function(){
      const total = $("#crash-multi-menu .crash-cb").length;
      const checked = $("#crash-multi-menu .crash-cb:checked").length;
      $("#crash-multi-all").prop("checked", total > 0 && checked === total);
      updateCrashTriggerText();
    });
    $("#crash-multi-menu").off("click.closeLocal").on("click.closeLocal", "#crash-multi-close", function(e){
      e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      $("#crash-multi-menu").hide();
      return false;
    });
    $("#crash-checkbox-mode-toggle").off("change").on("change", function(){
       const on = this.checked;
       $("#crash-select").prop("disabled", on);
       $("#crash-checkbox-select-all").prop("disabled", !on);
       if (on) {
         // 复选模式默认不全选
         $("#crash-checkbox-select-all").prop("checked", false);
         $("#crash-checkbox-list .crash-cb").prop("checked", false);
       }
     });
     $("#crash-checkbox-select-all").off("change").on("change", function(){
       const on = this.checked;
       if (!$("#crash-checkbox-mode-toggle").prop("checked")) return;
       $("#crash-checkbox-list .crash-cb").prop("checked", on);
     });
    $("#profile-list-button").off("click").on("click", function(e){ e.preventDefault(); profileList(); });
    $("#profile-remove-button").off("click").on("click", function(e){ e.preventDefault(); profileRemove(); });
    $("#assistive-enable-button").off("click").on("click", function(e){ e.preventDefault(); assistiveAction("enable"); });
    $("#assistive-disable-button").off("click").on("click", function(e){ e.preventDefault(); assistiveAction("disable"); });
    $("#apps-running-button").off("click").on("click", function(e){ e.preventDefault(); appsRunning(); });
    $("#syslog-start-button").off("click").on("click", function(e){ e.preventDefault(); if (!ensureDeviceSelected()) return; syslogStart(); });
    $("#syslog-stop-button").off("click").on("click", function(e){ e.preventDefault(); if (!ensureDeviceSelected() || !ensureSyslogStarted()) return; syslogStop(); });
    // Profile 多选触发
    $("#profile-multi-trigger").off("click").on("click", function(e){
      e.preventDefault(); e.stopPropagation();
      const $m = $("#profile-multi-menu");
      $m.toggle();
      return false;
    });
    // 点击空白隐藏
    bindDocGlobal("click", "profilemenu", function(){ $("#profile-multi-menu").hide(); });
    // 菜单内部不冒泡
    $("#profile-multi-menu").off("click").on("click", function(e){ e.stopPropagation(); });
    // 全选
    bindDocNS("change", "profilemenu", "#profile-multi-all", function(){
      const on = this.checked;
      $("#profile-multi-menu .profile-cb").prop("checked", on);
      updateProfileTriggerText();
    });
    // 清空（使用本地委托，避免被容器 stopPropagation 影响）
    $("#profile-multi-menu").off("click.clearLocal").on("click.clearLocal", "#profile-multi-clear", function(e){
      e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      $("#profile-multi-menu .profile-cb").prop("checked", false);
      $("#profile-multi-all").prop("checked", false);
      $("#profile-select").val([]);
      updateProfileTriggerText();
      $("#profile-multi-menu").hide();
      return false;
    });
    // 单个复选
    bindDocNS("change", "profilecb", "#profile-multi-menu .profile-cb", function(){
      const total = $("#profile-multi-menu .profile-cb").length;
      const checked = $("#profile-multi-menu .profile-cb:checked").length;
      $("#profile-multi-all").prop("checked", total > 0 && checked === total);
      updateProfileTriggerText();
    });
    // 关闭按钮
    $("#profile-multi-menu").off("click.closeLocal").on("click.closeLocal", "#profile-multi-close", function(e){
      e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      $("#profile-multi-menu").hide();
      return false;
    });
  }

  // =========================
  // 事件绑定（仅 JS；HTML 不写 onclick）
  // =========================
  function bindEvents() {
    $("#device-select").off("change").on("change", function() {
      syncHiddenUdid();
      const selectedUdid = getUDID();
      
      // 设备选择变化时的处理
      if (!selectedUdid) {
        // 没有选择设备
        showDevModeStatus("no-device");
      } else {
        // 选择了设备，延迟检测开发者模式
        setTimeout(backgroundDevModeCheck, 500);
      }
    });

    $("#refresh-devices-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); refreshDevices();
    });

    $("#get-device-info-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); getDeviceInfo();
    });



    $("#take-screenshot-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); takeScreenshot();
    });

    $("#record-screen-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); toggleScreenRecord();
    });

    // 新增：应用管理
    $("#get-installed-apps-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); getInstalledApps();
    });

    $("#app-search-input").off("input").on("input", filterAppsBySearch);

    $("#stop-app-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); stopApp();
    });

    $("#install-ipa-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); installIpa();
    });

    $("#launch-app-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); launchApp();
    });

    $("#reboot-device-button").off("click").on("click", function (e) {
      e.preventDefault(); e.stopImmediatePropagation(); rebootDevice();
    });
  }

  // —— 初始化 ——
  $(function () {
    $("#loading-overlay").hide();
    bindEvents();
    bindDMEvents();
    bindDeviceEventsSSE();

    // 初始化时隐藏开发者模式状态提示
    showDevModeStatus(null);

    refreshDevices();
  });

  // 暴露少量接口（调试可用）
  const IOSApp = {
    refreshDevices, getDeviceInfo, takeScreenshot,
    getInstalledApps, stopApp, installIpa
  };
  global.IOSApp = IOSApp;

  function updateCrashTriggerText() {
    const sels = [];
    $("#crash-multi-menu .crash-cb:checked").each(function(){ sels.push(this.value); });
    const txt = sels.length ? `${sels.length} 项已选` : "点击选择日志👇（支持多选）";
    $("#crash-multi-trigger").text(txt);
  }

  function updateProfileTriggerText() {
    const sels = [];
    $("#profile-multi-menu .profile-cb:checked").each(function(){ sels.push(this.value); });
    const txt = sels.length ? `${sels.length} 项已选` : "点击选择配置👇（支持多选）";
    $("#profile-multi-trigger").text(txt);
  }

  // 绑定筛选与清空
    $(document).off('click.syslogFilter').on('click.syslogFilter', '#syslog-apply-filter', function(e){
    e.preventDefault();
    
    // 1. 检查设备是否连接
    if (!ensureDeviceSelected()) return;
    
    // 2. 检查关键字输入是否为空
    const kw = (document.getElementById('syslog-kw')?.value || '').trim();
    const lv = (document.getElementById('syslog-lv')?.value || '').trim();
    
    if (!kw && !lv) {
      return alertAndHide({ icon: 'warning', title: '请输入筛选条件', text: '请在关键字输入框中输入筛选条件或选择日志级别', timer: 2000 });
    }
    
    // 3. 检查是否已开始系统日志（可选，不影响筛选功能）
    if (!ensureSyslogStarted()) return;
    
    // 应用筛选：清空显示内容，重新应用筛选条件到现有日志
    _syslogGen++;
    syslogClearContentOnly();
    
    // 重新处理现有缓冲区中的日志，应用新的筛选条件
    
    // 如果有筛选条件，重新处理缓冲区
    if (kw || lv) {
      const filteredLines = [];
      for (const line of _syslogBuf) {
        let include = true;
        
        // 关键字筛选
        if (kw) {
          const keywords = kw.split(',').map(k => k.trim().toLowerCase());
          const lineLower = line.toLowerCase();
          include = keywords.some(keyword => lineLower.includes(keyword));
        }
        
        // 级别筛选 - 适配原始 syslog 格式
        if (include && lv) {
          const level = lv.toLowerCase();
          const lineLower = line.toLowerCase();
          if (level === 'error') {
            include = lineLower.includes('error') || lineLower.includes('fatal') || lineLower.includes('critical') || lineLower.includes('<error>');
          } else if (level === 'warning') {
            include = lineLower.includes('warning') || lineLower.includes('warn') || lineLower.includes('<warning>');
          } else if (level === 'info') {
            include = lineLower.includes('info') || lineLower.includes('notice') || lineLower.includes('<notice>') || lineLower.includes('<info>');
          }
        }
        
        if (include) {
          filteredLines.push(line);
        }
      }
      
      // 显示筛选后的日志
      const $out = $("#syslog-live");
      if ($out.length) {
        const el = $out[0];
        for (const line of filteredLines) {
          const div = document.createElement('div');
          div.textContent = line;
          el.appendChild(div);
        }
        el.scrollTop = el.scrollHeight;
      }
    } else {
      // 无筛选条件，显示所有日志
      const $out = $("#syslog-live");
      if ($out.length) {
        const el = $out[0];
        for (const line of _syslogBuf) {
          const div = document.createElement('div');
          div.textContent = line;
          el.appendChild(div);
        }
        el.scrollTop = el.scrollHeight;
      }
    }
  });

      $(document).off('click.syslogClear').on('click.syslogClear', '#syslog-clear-live', function(e){
      e.preventDefault();
      if (!ensureDeviceSelected() || !ensureSyslogStarted()) return;
      _syslogBuf.length = 0; _syslogGen++;
      syslogClearContentOnly();
    });

  // =========================
  // 后台自动检测开发者模式
  // =========================
  
  // 检测设备连接状态
  async function checkDeviceConnection(udid) {
    try {
      const res = await fetch(`${API.getDeviceInfo}?udid=${encodeURIComponent(udid)}`, { cache: "no-store" });
      return res.ok;
    } catch (e) {
      return false;
    }
  }

  // 检测开发者模式状态
  async function checkDevModeStatus(udid) {
    try {
      const res = await fetch(`${API.devmodeCheck}?udid=${encodeURIComponent(udid)}`, { cache: "no-store" });
      
      if (!res.ok) {
        return null;
      }
      
      const data = await res.json();
      
      if (!data.ok) {
        console.log("checkDevModeStatus: API返回失败", data.raw);
        return null;
      }
      
      const raw = String(data.raw || "").toLowerCase().trim();
      
      // 更精确的匹配逻辑
      if (raw.includes("enabled: true") || raw.includes("enabled: 1") || raw.includes("enabled: yes")) {
        return "enabled";
      } else if (raw.includes("enabled: false") || raw.includes("enabled: 0") || raw.includes("enabled: no")) {
        return "disabled";
      } else if (raw.includes("disabled: true") || raw.includes("disabled: 1")) {
        return "disabled";
      } else if (raw.includes("disabled: false") || raw.includes("disabled: 0")) {
        return "enabled";
      }
      
      // 兜底匹配
      if (raw.includes("true") && !raw.includes("false")) {
        return "enabled";
      } else if (raw.includes("false") && !raw.includes("true")) {
        return "disabled";
      }
      
      return null;
    } catch (e) {
      console.error("checkDevModeStatus: 检测异常", e);
      return null;
    }
  }

  // 显示开发者模式状态提示
  function showDevModeStatus(status, message) {
    const $tip = $("#devmode-status-tip");
    if (status === "enabled") {
      $tip.html('<span class="text-success">✓ 开发者模式已启用</span>').show();
    } else if (status === "disabled") {
      $tip.html('<span class="text-warning">⚠️ 开发者模式未启用</span>').show();
    } else if (status === "error") {
      $tip.html(`<span class="text-danger">❌ 检测失败: ${message}</span>`).show();
    } else if (status === "checking") {
      $tip.html(`<span class="text-info">🔄 ${message}</span>`).show();
    } else if (status === "no-device") {
      $tip.html('<span class="text-muted">请连接设备</span>').show();
    } else {
      $tip.hide();
    }
  }

  // 检测所有设备的开发者模式状态
  async function checkAllDevicesDevMode() {
    const $select = $("#device-select");
    const selectedUdid = getUDID();
    
    // 如果没有选择设备，隐藏状态提示
    if (!selectedUdid) {
      showDevModeStatus("no-device");
      return;
    }

    // 检查设备连接状态
    const isConnected = await checkDeviceConnection(selectedUdid);
    if (!isConnected) {
      showDevModeStatus("error", "设备连接异常");
      return;
    }
    showDevModeStatus("checking", "正在检测开发者模式...");

    try {
      const devModeStatus = await checkDevModeStatus(selectedUdid);
      
      if (devModeStatus === "disabled") {
        showDevModeStatus("disabled");
        // 开发者模式未启用，提醒用户手动开启
        await alertAndHide({
          icon: "warning",
          title: "开发者模式未启用",
          text: "当前设备未启用开发者模式，某些调试功能可能无法正常使用。请在设备上手动开启开发者模式。",
          timer: 4000
        });
      } else if (devModeStatus === "enabled") {
        showDevModeStatus("enabled");
      } else {
        showDevModeStatus("error", "无法检测开发者模式状态");
      }
      
    } catch (e) {
      console.error("checkAllDevicesDevMode: 检测异常", e);
      showDevModeStatus("error", e.message);
    }
  }

  // 后台自动检测流程（重命名保持兼容）
  async function backgroundDevModeCheck() {
    await checkAllDevicesDevMode();
  }

  // 公共：系统日志占位与内容管理
  function syslogShowPlaceholder() {
    const $box = $("#syslog-live");
    if ($box.length) { $("#syslog-placeholder").show(); }
  }
  function syslogHidePlaceholder() {
    const $box = $("#syslog-live");
    if ($box.length) { $("#syslog-placeholder").hide(); }
  }
  function syslogClearContentOnly() {
    const $box = $("#syslog-live");
    if ($box.length) { $box.find('div:not(#syslog-placeholder)').remove(); $box[0].scrollTop = 0; }
  }

  // 公共：操作前置校验
  function ensureDeviceSelected() {
    const udid = getUDID();
    if (!udid) { alertAndHide({ icon: 'warning', title: '请选择设备', timer: 1500 }); return false; }
    return true;
  }
  function ensureSyslogStarted() {
    if (!_syslogSession) { alertAndHide({ icon: 'warning', title: '请先开始系统日志', timer: 1500 }); return false; }
    return true;
  }

  // =========================
  // Debug功能：三连击触发日志导出
  // =========================
  
  // Debug点击计数器
  let debugClickCount = 0;
  let debugClickTimer = null;
  const DEBUG_CLICK_TIMEOUT = 2000; // 2秒内需要完成三连击
  const DEBUG_CLICK_REQUIRED = 3;   // 需要3次点击
  
  // 初始化Debug触发器
  function initDebugTrigger() {
    const $trigger = $("#debug-trigger");
    if (!$trigger.length) return;
    
    $trigger.on("click", function(e) {
      e.preventDefault();
      debugClickCount++;
      
      // 清除之前的定时器
      if (debugClickTimer) {
        clearTimeout(debugClickTimer);
      }
      
      // 设置新的定时器
      debugClickTimer = setTimeout(() => {
        // 超时重置计数器
        debugClickCount = 0;
        debugClickTimer = null;
      }, DEBUG_CLICK_TIMEOUT);
      
      // 检查是否达到三连击
      if (debugClickCount >= DEBUG_CLICK_REQUIRED) {
        // 重置计数器
        debugClickCount = 0;
        if (debugClickTimer) {
          clearTimeout(debugClickTimer);
          debugClickTimer = null;
        }
        
        // 触发debug功能
        triggerDebugMenu();
      }
    });
  }
  
  // 触发Debug功能选择
  async function triggerDebugMenu() {
    try {
      const result = await Swal.fire({
        title: 'Debug功能',
        text: '请选择要执行的调试操作',
        icon: 'question',
        showCancelButton: true,
        confirmButtonText: '导出日志',
        cancelButtonText: '取消',
        showDenyButton: true,
        denyButtonText: '清理进程',
        reverseButtons: true
      });
      
      if (result.isConfirmed) {
        await triggerDebugExport();
      } else if (result.isDenied) {
        await triggerDebugCleanup();
      }
    } catch (error) {
      console.error('Debug菜单异常:', error);
    }
  }
  
  // 触发Debug日志导出
  async function triggerDebugExport() {
    try {
      // 显示确认对话框
      const result = await Swal.fire({
        title: '导出日志',
        text: '即将导出应用日志文件用于问题分析，是否继续？',
        icon: 'question',
        showCancelButton: true,
        confirmButtonText: '导出日志',
        cancelButtonText: '取消',
        reverseButtons: true
      });
      
      if (!result.isConfirmed) {
        return;
      }
      
      // 显示加载状态
      Swal.fire({
        title: '正在导出日志...',
        text: '请稍候，正在读取和复制日志文件',
        allowOutsideClick: false,
        didOpen: () => {
          Swal.showLoading();
        }
      });
      
      // 调用API导出日志
      const response = await fetch(API.debugExportLogs, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        }
      });
      
      const data = await response.json();
      
      if (data.ok) {
        // 导出成功，显示下载链接
        await Swal.fire({
          title: '日志导出成功',
          html: `
            <p>日志文件已成功导出：</p>
            <p><strong>${data.filename}</strong></p>
            <p class="text-muted small">文件已保存到下载目录，1小时内有效</p>
          `,
          icon: 'success',
          confirmButtonText: '下载文件',
          showCancelButton: true,
          cancelButtonText: '关闭'
        }).then((result) => {
          if (result.isConfirmed && data.download_url) {
            // 触发下载
            const link = document.createElement('a');
            link.href = data.download_url;
            link.download = data.filename;
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
          }
        });
      } else {
        // 导出失败
        await Swal.fire({
          title: '导出失败',
          text: data.msg || '未知错误',
          icon: 'error',
          confirmButtonText: '确定'
        });
      }
      
    } catch (error) {
      console.error('Debug导出异常:', error);
      await Swal.fire({
        title: '导出异常',
        text: '网络错误或服务器异常，请稍后重试',
        icon: 'error',
        confirmButtonText: '确定'
      });
    }
  }
  
  // 触发Debug进程清理
  async function triggerDebugCleanup() {
    try {
      // 显示确认对话框
      const result = await Swal.fire({
        title: '清理进程',
        text: '即将清理所有遗留的ios.exe进程，是否继续？',
        icon: 'warning',
        showCancelButton: true,
        confirmButtonText: '清理进程',
        cancelButtonText: '取消',
        reverseButtons: true
      });
      
      if (!result.isConfirmed) {
        return;
      }
      
      // 显示加载状态
      Swal.fire({
        title: '正在清理进程...',
        text: '请稍候，正在终止遗留的ios.exe进程',
        allowOutsideClick: false,
        didOpen: () => {
          Swal.showLoading();
        }
      });
      
      // 调用API清理进程
      const response = await fetch(API.debugCleanupProcesses, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        }
      });
      
      const data = await response.json();
      
      if (data.ok) {
        await Swal.fire({
          title: '进程清理成功',
          text: data.msg || '所有遗留的ios.exe进程已清理',
          icon: 'success',
          confirmButtonText: '确定'
        });
      } else {
        await Swal.fire({
          title: '清理失败',
          text: data.msg || '未知错误',
          icon: 'error',
          confirmButtonText: '确定'
        });
      }
      
    } catch (error) {
      console.error('Debug清理异常:', error);
      await Swal.fire({
        title: '清理异常',
        text: '网络错误或服务器异常，请稍后重试',
        icon: 'error',
        confirmButtonText: '确定'
      });
    }
  }
  
  // 初始化引导文档入口
  function initGuideEntry() {
    const $guideBtn = $("#guide-entry-btn");
    if (!$guideBtn.length) return;
    
    $guideBtn.on("click", function(e) {
      e.preventDefault();
      openGuideDocument();
    });
  }
  
  // 打开引导文档
  function openGuideDocument() {
    // 在新窗口中打开引导文档
    const guideUrl = "/guide";
    window.open(guideUrl, "_blank", "width=1200,height=800,scrollbars=yes,resizable=yes");
  }
  
  // 页面加载完成后初始化Debug功能
  $(document).ready(function() {
    initDebugTrigger();
    initGuideEntry();
  });

})(window);