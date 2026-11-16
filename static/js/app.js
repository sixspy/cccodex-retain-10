(() => {
  const main = document.getElementById("main");
  let view = "policies";

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁"
          ? "ok"
          : state === "已失败"
            ? "bad"
            : "";
    return `<span class="tag ${cls}">${state}</span>`;
  }

  function progressTag(state) {
    const cls =
      state === "水线内已齐" || state === "水线内已完成"
        ? "ok"
        : state === "水线内应裁未裁" || state === "水线内未执行" || state === "完成但实物不符"
          ? "bad"
          : state === "已裁且水线外" || state === "水线外未齐" || state === "水线外待执行"
            ? "warn"
            : "";
    return `<span class="tag ${cls}">${state}</span>`;
  }

  let toastTimer = null;
  function notice(message, kind = "ok") {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      document.body.appendChild(el);
    }
    el.className = `toast ${kind}`;
    el.textContent = message;
    el.style.display = "block";
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.style.display = "none";
    }, kind === "bad" ? 9000 : 4000);
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td>${p.domain_name}</td>
              <td class="mono">${p.domain_code}</td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const asOf = document.getElementById("part-as-of")?.value.trim() || "2026-11-16";
    const data = await api(
      `/api/partitions?as_of_date=${encodeURIComponent(asOf)}`
    );
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">水线基准日
          <input id="part-as-of" value="${asOf}" />
        </label>
        <button type="button" class="secondary" id="btn-part-refresh">按基准日刷新</button>
      </div>
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。水线列与进度卡片同源；「水线内应裁未裁」即账实已偏，必须查。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th><th>水线视角</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${p.domain_code}</td>
              <td class="mono">${p.biz_date}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}</td>
              <td class="mono">${p.archive_uri || "—"}</td>
              <td title="${p.progress_note || ""}">
                ${progressTag(p.progress_state)}
                <div class="hint-line">${p.progress_note || ""}</div>
              </td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-part-refresh").onclick = renderPartitions;
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.as_of_date}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${j.message}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="5" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const asOf = document.getElementById("jud-as-of")?.value.trim() || "2026-11-16";
    const data = await api(
      `/api/judgments?as_of_date=${encodeURIComponent(asOf)}`
    );
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">水线基准日
          <input id="jud-as-of" value="${asOf}" />
        </label>
        <button type="button" class="secondary" id="btn-jud-refresh">按基准日刷新</button>
      </div>
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">待执行可点「执行」。执行结果写回分区存储态与执行流水；水线列与进度卡片同源。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>水线视角</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.domain_code}</td>
              <td class="mono">${j.biz_date}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${j.decided_action}</td>
              <td>${tag(j.status)}</td>
              <td title="${j.progress_note || ""}">
                ${progressTag(j.progress_state)}
                <div class="hint-line">${j.progress_note || ""}</div>
              </td>
              <td>${
                j.status === "待执行" || j.status === "已失败"
                  ? `<button type="button" data-exec="${j.id}">执行</button>`
                  : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-jud-refresh").onclick = renderJudgments;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          const res = await api(
            `/api/judgments/${btn.dataset.exec}/execute`,
            { method: "POST" }
          );
          notice(`裁决 #${btn.dataset.exec} 执行完成：${res.run.result_state}`);
        } catch (err) {
          notice(`执行失败：${err.message}`, "bad");
        }
        await renderJudgments();
      };
    });
  }

  async function renderProgress() {
    const asOf = document.getElementById("prog-as-of")?.value.trim() || "2026-11-16";
    const [data, marks, logs] = await Promise.all([
      api(`/api/progress-cards?as_of_date=${encodeURIComponent(asOf)}`),
      api("/api/watermarks"),
      api("/api/progress-logs"),
    ]);
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">水线基准日
          <input id="prog-as-of" value="${asOf}" />
        </label>
        <button type="button" class="secondary" id="btn-prog-refresh">按基准日刷新</button>
      </div>
      <section class="panel">
        <h2>裁撤进度水线</h2>
        <p class="sub">每主题域按到期动作分线（归档 / 销毁互斥）。水线 = 已连续完成裁撤的末日；完成数只认分区实物与已完成裁决留痕同源结果。</p>
        <div class="cards">
          ${
            data.items.length
              ? data.items.map(renderProgressCard).join("")
              : `<div class="empty">暂无主题域策略</div>`
          }
        </div>
      </section>
      <section class="panel">
        <h2>水位快照台账（落库可核）</h2>
        <p class="sub">append-only 不可变快照；每次推进追加一条，失败不写快照，另见推进留痕。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>动作</th><th>策略版本</th><th>序号</th><th>水线</th><th>完成数</th><th>到期线</th><th>基准日</th><th>落库时间</th></tr></thead>
          <tbody>
            ${
              marks.items.length
                ? marks.items
                    .map(
                      (m) => `<tr>
              <td>${m.id}</td>
              <td class="mono">${m.domain_code}</td>
              <td>${m.action_on_expire}</td>
              <td>v${m.policy_version}</td>
              <td>${m.seq}</td>
              <td class="mono">${m.watermark_date}</td>
              <td>${m.completed_count}</td>
              <td class="mono">${m.cutoff_date}</td>
              <td class="mono">${m.as_of_date}</td>
              <td class="mono">${m.created_at}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="10" class="empty">尚无水位快照</td></tr>`
            }
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>水位推进留痕</h2>
        <p class="sub">成功与失败均留痕；失败行给出中文原因，零快照写入。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>动作</th><th>基准日</th><th>结果</th><th>水线前→后</th><th>完成数</th><th>说明</th><th>时间</th></tr></thead>
          <tbody>
            ${
              logs.items.length
                ? logs.items
                    .map(
                      (l) => `<tr>
              <td>${l.id}</td>
              <td class="mono">${l.domain_code}</td>
              <td>${l.action_on_expire || "—"}</td>
              <td class="mono">${l.as_of_date}</td>
              <td>${l.ok ? `<span class="tag ok">成功</span>` : `<span class="tag bad">失败</span>`}</td>
              <td class="mono">${l.before_watermark || "未立线"} → ${l.after_watermark || "—"}</td>
              <td>${l.completed_count}</td>
              <td>${l.message}</td>
              <td class="mono">${l.created_at}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="9" class="empty">尚无推进留痕</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-prog-refresh").onclick = renderProgress;
    main.querySelectorAll("[data-advance]").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const res = await api(
            `/api/progress-cards/${btn.dataset.advance}/advance`,
            { method: "POST", body: JSON.stringify({ as_of_date: asOf }) }
          );
          notice(res.message);
        } catch (err) {
          notice(`进度推进失败：${err.message}`, "bad");
        }
        await renderProgress();
      };
    });
  }

  function renderProgressCard(c) {
    const unfinished = c.unfinished.length
      ? `<details ${c.unfinished_count ? "open" : ""}>
          <summary>未齐 ${c.unfinished_count} 条</summary>
          <table class="inner">
            <thead><tr><th>分区</th><th>业务日</th><th>存储态</th><th>行数</th><th>账实</th><th>原因</th></tr></thead>
            <tbody>
              ${c.unfinished
                .map(
                  (u) => `<tr class="${u.blocks_watermark ? "blocking" : ""}">
                    <td class="mono">#${u.partition_id}</td>
                    <td class="mono">${u.biz_date}</td>
                    <td>${tag(u.storage_state)}</td>
                    <td>${u.row_count}</td>
                    <td>${u.physical_done ? "实物齐" : "实物未齐"} / ${u.ledger_done ? "有留痕" : "无留痕"}</td>
                    <td>${u.blocks_watermark ? `<span class="tag bad">拦水线</span> ` : ""}${u.reason}</td>
                  </tr>`
                )
                .join("")}
            </tbody>
          </table>
        </details>`
      : `<div class="ok-line">到期分区已全部齐备</div>`;

    const frozen = c.frozen_lines.length
      ? `<div class="frozen">
          <strong>已封存旧水线</strong>
          ${c.frozen_lines
            .map(
              (f) =>
                `<span class="tag">${f.action_on_expire} v${f.policy_version} · 水线 ${f.watermark_date} · 完成 ${f.completed_count}</span>`
            )
            .join(" ")}
        </div>`
      : "";

    return `
      <article class="card ${c.can_advance ? "ready" : ""}">
        <header>
          <div>
            <h3>${c.domain_name} <span class="mono">${c.domain_code}</span></h3>
            <div class="hint-line">
              动作 ${tag(c.action_on_expire)} · 保留 ${c.keep_days} 天 · 策略 v${c.policy_version}
              · 到期线 ${c.cutoff_date}
            </div>
          </div>
          <button type="button" data-advance="${c.domain_code}" ${
            c.can_advance ? "" : "disabled"
          }>推进水线</button>
        </header>
        <div class="meter">
          <div class="meter-row">
            <span>当前水线</span>
            <strong class="mono">${c.watermark_date || "未立线"}</strong>
          </div>
          <div class="meter-row">
            <span>水线完成数 / 已到期</span>
            <strong>${c.watermark_completed} / ${c.eligible_total}</strong>
          </div>
          <div class="meter-row">
            <span>可推进至</span>
            <strong class="mono">${c.candidate_date || "—"}</strong>
          </div>
          <div class="meter-row">
            <span>未齐条</span>
            <strong class="${c.unfinished_count ? "bad-text" : "ok-text"}">${c.unfinished_count}</strong>
          </div>
          ${
            c.destroy_pending_count
              ? `<div class="meter-row"><span>销毁待清</span><strong class="bad-text">${c.destroy_pending_count}</strong></div>`
              : ""
          }
          <div class="meter-row">
            <span>账实不符</span>
            <strong class="${c.anomaly_count || c.count_mismatch ? "bad-text" : ""}">
              ${c.anomaly_count}${c.count_mismatch ? " · 水位数不同源" : ""}
            </strong>
          </div>
          <div class="meter-row">
            <span>快照序号</span>
            <strong>${c.watermark_seq}</strong>
          </div>
        </div>
        ${
          c.block_reason
            ? `<div class="block-reason">${c.can_advance ? "" : "⛔ "}${c.block_reason}</div>`
            : `<div class="ok-line">水线可推进：${c.watermark_date || "未立线"} → ${c.candidate_date}</div>`
        }
        ${unfinished}
        ${frozen}
      </article>`;
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${r.as_of_date}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count}</td>
              <td>${r.summary}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    progress: renderProgress,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
