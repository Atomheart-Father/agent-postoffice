import {appendFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {homedir} from 'node:os';
import {basename, dirname, join, resolve} from 'node:path';
import {sessionIdentity} from './office_pi_session.mjs';
import {readDescriptor, createPiRoundClient, createPiHostEvents, releaseVersion} from './office_pi_round.mjs';
// 本宿主的薄适配器：共享工具表 + 共享调用路径，登记成原生 Pi 工具（直接 `-e` 与安装两种形态同一个文件）。
import officePiTools from './office_pi_tools.ts';
import {createWakeLoop} from './office_wake_loop.mjs';
import {createOfficeSocket} from './office_socket.mjs';
// “哪些名字是本扩展的工具”只有一处答案：共享 spec 的 `TOOL_NAMES`（本宿主适配器注册时用的同一份）。
import {TOOL_NAMES} from './office_tool_spec.mjs';
import TOOL_TEXT from './office_tool_text.json' with { type: 'json' };



// 本文件的模块一律按**实际加载的这一份**解析（下面的相对 import）：安装形态下 Pi 加载的
// `extensions/office-tools.ts` 再导出释出来就是这份文件，所以"装的是哪棵树"不需要再从安装桩里读回来
// 猜一遍——那段读回来再和自身比较的代码只服务于一个永远不成立的分叉（`code` 算出来只进了一个总为真的守卫）。

/**
 * 本文件**唯一**的诊断写口：只进那份 opt-in 文件，不写终端；没开诊断就什么都不写。
 *
 * 放在模块顶层而不是某个闭包里：它的读者分属两个闭包（唤醒循环的 `diag`/`diagFailure` 在
 * `wireBoundFeatures` 里，工具面的 `dbg` 在外层），而它自己只读 `W289_DEBUG` 与 `appendFileSync`。
 * 上次把它塞进内层，`dbg` 那一处就成了运行期才炸的 `wakeLog is not defined`。
 */
const wakeLog = (line: string, extra?: Record<string, unknown>) => {
  try {
    const f = process.env.W289_DEBUG;
    if (!f) return;
    appendFileSync(f, JSON.stringify({at: Date.now(), msg: line, ...(extra || {})}) + '\n');
  } catch { /* 诊断不影响行为 */ }
};

// One installed native Pi extension. Providers, settings, themes and sessions
// remain Pi's; this extension adds the Office tools and its native mail lifecycle.
export default async function nativeOffice(pi: any, options: {home?: string} = {}) {
  // ★ W436：安装器写入口时传的就是 **Office home**（`postoffice` 的 `HOME`，即 `POSTOFFICE_HOME`
  //   的缺省 `~/agent-postoffice`）—— 与这里直接收一个显式 Office home 是同一件事实。
  //   这里曾经还拿 `sameTree(options.home, PI_CODING_AGENT_DIR || ~/.pi/agent)` 过滤一遍，
  //   把"恰好等于 Pi agent 目录的那个 `home`"当成安装器残留丢掉。但安装器写的从来不是
  //   agent 目录（它写 `HOME`），即那条过滤只对**旧形状**生效，属于为旧格式保兼容：删掉，
  //   现在 `home` 只有一个来源顺序（显式选项 → `POSTOFFICE_HOME` → 缺省）。
  const home = options.home || process.env.POSTOFFICE_HOME || join(homedir(), 'agent-postoffice');
  Object.assign(process.env, {
    POSTOFFICE_HOME: home, PI_STAFF_ROOT: home,
  });
  // Retire the legacy checkout selectors; installed entries, not inherited env or this loaded file,
  // are the only source for the modules that provide Office host behavior.
  delete process.env.PI_STAFF_CODE;
  delete process.env.PI_STAFF_PO;
  // ★ W158 R3.4（退回意见 1）：**继承来的 env 一律不许凌驾身份**。
  //
  //   现场：在役 E/D 版本的扩展会在 `session_start` 自己写 `OFFICE_ENDPOINT_FILE`，而它
  //   **没有**任何"这值是我写的"标记（本版也不给继承值发标记）。正常 `/reload` 迁到新版后，
  //   那个继承值会被当成"宿主显式指定"永久保留；随后 `/new` 换到另一个会话/箱子，端点还指着旧身份。
  //   所以这里不去猜"谁写的"：原生 Pi 里**只看当前真实会话的唯一映射**。
  //     · 唯一映射 ⇒ 指过去；
  //     · 未登记 / 有歧义 ⇒ **删掉**（不吃旧 descriptor、不猜、不扫目录、不找旧树）。
  //   其它工具宿主显式提供的连接由共享工具层处理；原生 Pi 只使用当前会话选出的端点。
  //
  const nativeSessionFile = (ctx: any): string => {
    try { return String(ctx?.sessionManager?.getSessionFile?.() || ''); } catch { return ''; }
  };
  /**
   * 本会话的引用，以及它**唯一**那条规则。
   *
   * native（`getSessionFile()`）是 daemon 的 `hostSessionRef` 必须等于的那个，`PI_SESSION_FILE` 是
   * **启动方**报给供给链的那个（runtime：先报路径 → 写供给 → 起 officed → 再核对）。同一件事实的
   * 两个来源，所以必须一致：两个都在、且**明确不同**（`different`）⇒ 没有绑定，而不是挑一个。
   * 只有 native 为空（宿主没有会话文件）时，reported 才顶上。
   */
  const sessionRefOf = (ctx: any): string => {
    const native = nativeSessionFile(ctx);
    const reported = String(process.env.PI_SESSION_FILE || '');
    if (native && reported && sessionIdentity(native, reported) === 'different') return '';
    return native || reported;
  };
  /**
   * This session's mailbox: **the descriptor the host delivered**, and only when its `hostSessionRef`
   * is this very session file.
   *
   * ★ W439：身份不再从 staff.json / routes / .allow 猜回来。那三份是文件，会陈旧，也答不出
   *   "它描述的是哪一次会话" —— 换绑之后一条旧记录能把旧会话继续连在端点上。这里的事实只有一条：
   *   actor/bindingId/generation/instance 由 listener 钉在 socket 上，`hostSessionRef` 由同一个
   *   daemon 写出；两者对上 ⇒ 本会话的绑定，对不上 ⇒ 没有绑定（不猜别的路径、不扫目录）。
   *
   * 为什么把它写成"信箱 + 为什么没有"：真机上四种空长得一模一样（都只是 `bound=`），
   * 于是“到底卡在哪一步”只能靠猜。这里每个分支都带一句可以贴进诊断的实话。
   */
  const endpointWhy = (ctx: any): {box: string; why: string} => {
    const file = process.env.OFFICE_ENDPOINT_FILE || '';
    const sessionFile = sessionRefOf(ctx);
    if (!file) return {box: '', why: 'no_delivered_endpoint'};
    if (!sessionFile) return {box: '', why: 'no_session_ref:' + JSON.stringify({native: nativeSessionFile(ctx), reported: String(process.env.PI_SESSION_FILE || '')})};
    let d: any = null, err = '';
    try { d = readDescriptor(file); } catch (e: any) { err = String((e && e.message) || e); }
    if (!d) return {box: '', why: 'descriptor_unreadable:' + file + (err ? ':' + err : '')};
    const ref = typeof d.hostSessionRef === 'string' ? d.hostSessionRef : '';
    if (!ref) return {box: '', why: 'descriptor_without_session_ref:' + file};
    const id = sessionIdentity(ref, sessionFile);
    return id === 'same' ? {box: String(d.actor || ''), why: ''}
      : {box: '', why: id + ':' + JSON.stringify({ref, sessionFile})};
  };
  const boxForSession = (ctx: any): string => endpointWhy(ctx).box;
  //: 最近一次"为什么没有端点"。它只是一句诊断，由 `dbg()` 随工具面那几行一起落地 ——
  //  这个位置比诊断写口更早，不许在这里就写文件。
  let endpointNote = '';
  /**
   * Keep the delivered descriptor while it is this session's binding; drop the name only on an
   * **explicit mismatch**.
   *
   * ★ W439 D3（真机）：这里原来在“选不出”时就把 `OFFICE_ENDPOINT_FILE` 删了。而 Pi 懒建会话
   *   文件（`session_start` 时还不在盘上），那一刻的空是**核不出来**，不是“别人的” ——
   *   删掉之后没有恢复路径，整场零连接。名字由交付方给，丢它得有个确定的事实：
   *   `different` 才删；`unknown` 保留，下一个安全点再核。
   */
  const keepDeliveredEndpoint = (_event: unknown, ctx: any) => {
    const file = process.env.OFFICE_ENDPOINT_FILE || '';
    if (!file) return;
    const v = endpointWhy(ctx);
    if (v.box) return;
    endpointNote = v.why;
    if (v.why.startsWith('different:')) delete process.env.OFFICE_ENDPOINT_FILE;
  };
  // Session data is only a descriptor filename hint; the live socket remains the authority.
  const prepareSessionEndpoint = async (ctx: any, selected = "", gen?: number) => {
    const entries = ctx?.sessionManager?.getBranch?.() || [];
    const hint = [...entries].reverse().find((e: any) => e.type === 'custom' && e.customType === 'office.endpoint')?.data?.file;
    const id = String(ctx?.sessionManager?.getSessionId?.() || '');
    const candidate = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id) ? join(home, 'sock', `pi-${id}.endpoint.json`) : '';
    const supplied = selected || process.env.OFFICE_ENDPOINT_FILE || '';
    const file = supplied || (typeof hint === 'string' && /^[A-Za-z0-9_.-]+\.endpoint\.json$/.test(hint)
      ? join(home, 'sock', hint) : candidate);
    if (!file || resolve(dirname(file)) !== resolve(home, 'sock')) return;
    const d = readDescriptor(file), session = sessionRefOf(ctx);
    if (!d || !session || sessionIdentity(d.hostSessionRef, session) !== 'same'
        || resolve(d.socketPath) !== resolve(home, 'sock', 'officed.sock')
        || !d.tokenFile || resolve(dirname(d.tokenFile)) !== resolve(home, 'sock')) return;
    const client = createPiRoundClient({socketPath: d.socketPath, tokenFile: d.tokenFile});
    let who: any;
    try { who = await client.context(); } finally { client.close(); }
    if ((gen !== undefined && gen !== surfaceGen) || !who || who.ok === false || who.actor !== d.actor
        || sessionIdentity(sessionRefOf(ctx), session) !== 'same') return;
    process.env.OFFICE_ENDPOINT_FILE = file;
    if (hint !== basename(file)) pi.appendEntry('office.endpoint', {file: basename(file)});
    return true;
  };
  // Select this session's controlled endpoint before the tools refresh their context.
  //
  // ★ W436：这里曾经还有第二个 `session_start` 处理器，用 `runpy` 起 `bin/office-staff` 的
  //   `ensure_native_index`，给「会话在 Pi 原生目录之外」的箱子补一条 `pi -r` 能看见的链接。
  //   那条反向调用已删：**建链接的是 `office-staff` 自己**，它在 `execvpe` 之前就调了同一个
  //   函数（`bin/office-staff` 先 `ensure_native_index` 再 `os.execvpe`），所以宿主的这一次
  //   调用要么已被先做过、要么（会话本就在原生目录）立即返回 —— 它只多起一个 python 进程
  //   把 900 行大 CLI 整个 `runpy` 一遍。宿主不再回头调大型 `postoffice`。
  pi.on('session_start', keepDeliveredEndpoint);
  pi.on('before_agent_start', keepDeliveredEndpoint);
  // ★ W158 R3.2：登记可能发生在**会话中途**（`postoffice add <名> --pi this` 是一次工具执行，
  //   它写的是 staff.json / routes / .allow —— 全是文件，本进程的 `OFFICE_ENDPOINT_FILE` 还是
  //   启动时那一次选的结果）。只在 session_start / before_agent_start 选端点 ⇒ 同一轮里紧接着
  //   调用的 Office 工具仍然 `no_controlled_endpoint`，于是模型开始翻 endpoint / 旧装目录。
  //
  //   工具完成与一轮结束都是**宿主自己的安全点**（不是轮询、不是模拟 session_start）：
  //   在这里重读一次"本人映射"，把端点指到本会话的箱子。仍然不猜：
  //     · 唯一映射 ⇒ 指过去；· 没有映射 / 有歧义 ⇒ 保持"没有"（不扫目录、不找旧树、不挑最新会话）。
  // ★ W289：以下这些"员工业务接线"只在**已绑定**时安装（见 loadFull）。
  const wireBoundFeatures = (host: any = pi, initialCtx?: any) => {
  const reselect = (_event: unknown, ctx: any) => { try { keepDeliveredEndpoint(_event, ctx); } catch { /* 安全点不改变任何判定 */ } };
  for (const ev of ['tool_result', 'turn_end', 'agent_end']) {
    try { host.on(ev as any, reselect); } catch { /* 宿主没有这个事件：只少一个安全点 */ }
  }
  // ★ W431/W436：**岗位信唤醒的客户端调用点**（A 2026-11-16：接真实 Pi 生命周期）。
  //
  //   服务端 `round.await` 已按 `carried_units(box)`（本箱 + 本会话承载的岗位，与建轮授权同一份
  //   Core 事实）逐单元各问一次 `delivery_rule` ⇒ **客户端不传 unit**，也不自己挑单元（传了就是
  //   在前端再复制一份选信规则，正是要避免的）。
  //
  //   调用点只能是**宿主自己的安全点**，且**不 await**（`turn_end` 里等 30s 就是自等，会把回合
  //   边界卡住）。做法：挂一次**短超时**的有界等待，结果只用来记诊断与触发下一次问 Core；
  //   真正的选信仍由 `round.begin` 决定（这里绝不宣布"有信/没信"）。
  //
  //   为什么不自己起计时器：那就是第二套状态机（`phase` 陈旧那次的教训）。等待由安全点驱动，
  //   没安全点就自然不等待 —— 宁可晚一点醒，不许自己造节拍。
  // ★ 会话级**持续**等待（A 2026-11-16：长期空闲后新信到达要能实际开始处理）。
  //
  //   我上一版是"安全点等 1.5 秒就完" —— 信在此之后落箱就再也收不到，且 wake 之后只记诊断。
  //   A 指出那是结构测试冒充完成，我核了，成立。现在按 Pi 文档允许的方式重建：
  //     · 资源在**会话开始**起（文档禁止在 factory 里起后台资源）；
  //     · `session_shutdown` **幂等**关闭（文档要求）；
  //     · 循环之间**有最小间隔**（我实测过：`awaitRound` 瞬时返回会满载空转直到 OOM）；
  //     · 空闲时收到 wake ⇒ 按 Core `round.begin` 原样发送唯一 batch text；先记录 anchor payload，
  //       exact before_agent_start 记 admission，同会话顺序 agent_start 记 run/accepted，settled 消费。
  let wakeLoop: {start: (ctx?: any) => unknown; stop: () => void; onWakeFrame?: (frame: any, ctx: any) => unknown;
    modelProgress?: (ev: any) => any; turnClosed?: (turnId: string) => any;
    reportUsage?: (entries: any[], actKey: string) => any} | null = null;
  let hostEvents: {observe: (kind: string, ev: any) => Promise<any>} | null = null;
  let currentTurnId = '';
  const toolsInFlight = new Map<string, {turnId: string; v: number; last: string;
    started?: Promise<any>; ending?: Promise<void>; outcome?: 'ok' | 'error'}>();
  let wakeLoopBox = '';
  let pendingAttemptKey = '';
  //: 本模块实例加载时（= 这一次回载的那一份产物）它的版本；没可信标识就为 null。
  const loadedVersion = releaseVersion(import.meta.url);
  const stopWakeLoop = () => {
    hostEvents = null;
    currentTurnId = '';
    toolsInFlight.clear();
    try { if (wakeLoop) wakeLoop.stop(); } catch { /* 关闭失败不许挡住退出 */ }
    wakeLoop = null;
    wakeLoopBox = '';
  };
  const startWakeLoop = (ctx: any) => {
    let box = '';
    try { box = String(boxForSession(ctx) || ''); } catch { return; }
    if (!box) { stopWakeLoop(); return; }          // 没有唯一身份 ⇒ 不猜、不等待
    if (wakeLoop && wakeLoopBox === box) return;   // 同一箱已在跑（幂等）
    stopWakeLoop();                                // 换箱：先收旧的
    let desc: any = null;
    // 端点描述就是宿主交付的那一份（路径由 runtime 给）；本宿主不按箱名去目录里挑。
    try { desc = readDescriptor(process.env.OFFICE_ENDPOINT_FILE || ''); } catch { desc = null; }
    if (!desc || typeof desc.socketPath !== 'string' || !desc.socketPath) {
      return;                                                                      // 端点还没好：留给下次安全点
    }
    let client: any = null;
    try {
      client = createPiRoundClient({
        socketPath: desc.socketPath,
        // ★ W436/v9（A 2026-11-16）：旧 `token_file` 下划线别名已删。
        //   现行写入器（officed `_write_descriptor` / provision `write_endpoint_descriptor`）
        //   写的就是 `tokenFile` —— 客户端只认它真正会收到的那个键。
        tokenFile: typeof desc.tokenFile === 'string' ? desc.tokenFile : undefined,
      });
    } catch { return; }
    try {
      wakeLoop = createWakeLoop({
        client,
        box,
        // ★ W439：唤醒循环的诊断默认是**空函数**（`diag = () => {}`）。它内部那些"为什么没起轮"
        //   的判断（payload/identity 投影不全、令牌缺、拒绝……）原来因此**完全不可见** ——
        //   真机上只看到"轮建了、但没有任何 agent_start"。接进同一个（opt-in）诊断文件。
        diag: (m: string) => wakeLog('wake: ' + String(m)),
        diagFailure: (m: string) => wakeLog('wake-fail: ' + String(m)),
        host: {
          restoreAdmission: true,
          receiptEntries: (eventCtx: any) => eventCtx?.sessionManager?.getBranch?.(),
          saveReceipt: (receipt: any) => host.appendEntry('office.receipt', receipt),
          canWake: () => ctx.isIdle?.() !== false,
          toolBoundary: true,
          // Idle wakes use the native custom message. Active mail is inserted at a tool boundary.
          triggerTurn: async (batchText: string, details: Record<string, unknown>) => {
            // 纯续工：把这次尝试的 attempt_key 记在这里，等原生 agent_start 时交回唤醒循环 ——
            // 它只认这一个（精确关联），不认"下一个起点"。
            pendingAttemptKey = String((details && (details as any).attempt_key) || '');
            const fn = (host as any).sendMessage;
            if (typeof fn !== 'function') throw new Error('host.sendMessage missing');
            const customType = details?.officeWorkContinuation === true
              ? 'office-work-continuation' : 'office-wake';
            fn.call(host, {customType, content: batchText, display: true, details},
              {triggerTurn: true, deliverAs: 'followUp'});
          },
          // ★ W439：这里原来只读原生 `getSessionFile()`，而它在真实形态里是**空串**（报路径走的是
          //   `PI_SESSION_FILE`）。唤醒循环的建轮前硬门要求 sessionFile 非空 ⇒ 于是"轮建了、却没有
          //   任何 agent_start"（实测：pre-send rejection was not confirmed: round.begin payload/identity
          //   projection incomplete）。会话引用只有**一条规则**，就是上面 `sessionRefOf()`，这里复用它。
          sessionRef: () => sessionRefOf(ctx),
          // Build the stable host instance component from Pi's current session id and cwd.
          instanceId: (eventCtx: any) => {
            // 同一件事：`getSessionId()` 也可能是空串（与 `getSessionFile()` 同源）。宿主实例标识
            // 必须非空（硬门要它），所以按**会同一条规则**取：sessionId → 会话引用 → 进程。
            try {
              const cwd = String(eventCtx?.cwd || process.cwd() || '');
              const sid = String(eventCtx?.sessionManager?.getSessionId?.() || '');
              const who = sid || nativeSessionFile(eventCtx) || sessionRefOf(eventCtx) || `pid-${process.pid}`;
              return cwd ? `pi:${who}@${cwd}` : `pi:${who}`;
            } catch { return `pi:pid-${process.pid}`; }
          },
        },
      });
      const loop = wakeLoop;              // 局部引用：TS 的 null 收窄不跨闭包
      // 观测口与客户端同生共死：没连上就没有观测，也不替哪条事实编造身份（身份只由 socket 的可信 ctx 定）。
      hostEvents = createPiHostEvents({client});
      const resourceEvents = hostEvents;
      // Pi exposes the exact SKILL.md path for skills loaded into this session.
      const reportSkills = async () => {
        const paths = new Set(host.getCommands().filter((c: any) => c.source === 'skill').map((c: any) => c.sourceInfo?.path));
        const reported = new Set<string>();
        const socket = createOfficeSocket({socketPath: desc.socketPath, tokenFile: desc.tokenFile});
        try {
          for (const state of ['Active', 'Submitted']) {
            let cursor: string | null = null;
            do {
              const page: any = await socket.handle({op: 'work.list', role: 'assigned', state, ...(cursor ? {cursor} : {})});
              if (!page || page.ok === false) return;
              for (const work of page.items || []) {
                const status: any = await socket.handle({op: 'resource.status', target_kind: 'work', target_ref: work.work_ref});
                for (const item of status?.items || []) {
                  if (resourceEvents !== hostEvents) return;
                  const {resource_ref, locator} = item;
                  if (item.kind !== 'skill' || item.digest || reported.has(resource_ref)
                      || resolve(locator) !== locator || basename(locator) !== 'SKILL.md') continue;
                  const loaded = paths.has(locator);
                  if (!loaded && item.loaded !== true) continue;
                  reported.add(resource_ref);
                  await resourceEvents.observe('resource', {fields: {resource_ref, loaded,
                    act_key: `pi:resource:${randomUUID()}`}});
                }
              }
              cursor = page.next_cursor || null;
            } while (cursor);
          }
        } finally { socket.close(); }
      };
      void reportSkills().catch((e) => wakeLog(`resource observation failed: ${String(e)}`));
      // ★ W3：这一份**实际加载**的产物自报的版本，每次起这条会话报一次（`loaded`）。
      //   有可信标识就发：Core 落 `binding_state.loaded` 并答 `changed/previous` —— 同一个进程换到 B
      //   （重载/新进程重新解析安装入口）就是 `changed:true`，同一版重开进程就是 `changed:false`。
      observe('loaded', { fields: { version: loadedVersion?.version ?? null } });
      wakeLoopBox = box;
      const started = loop ? loop.start(ctx) : null;
      void Promise.resolve(started).catch(() => { /* 循环内部已自行记诊断 */ });
    } catch (e) {
      wakeLoop = null;
      wakeLoopBox = '';
    }
  };
  // ★ W436：Pi 1.5 单轮顺序是 before_agent_start → agent_start → agent_settled.
  //   精确 before-start 只记 admission；同会话紧随其后的 agent_start 才记 run/accepted。
  //   prepareRun 若失败不会有 settled；后续不匹配 before-start 将旧关联留作 unknown。Pi 原始 aborted 事实进入 consume.
  // 同一个写口（`wakeLog`）两个说法：一条观测为什么没发出去，不是第二个日志实现。
  const obsFailure = (code: string, message: string) => wakeLog(`host.event not sent (${code}): ${message}`);
  // 观测只是观测：结果只用来记一行诊断，既不挡住那句 `await` 之外的东西，也不擅自改写回合状态。
  const observeOutcome = (make: () => any) => {
    let out: any;
    try { out = make(); } catch (e: any) { obsFailure('observe_threw', String((e && e.message) || e)); return; }
    if (!out || typeof out.then !== 'function') return;
    return out.then(
      (r: any) => {
        if (r?.ok === false) obsFailure(String(r.code || 'refused'), String(r.message || ''));
        return r;
      },
      (e: any) => obsFailure('observe_threw', String((e && e.message) || e)),
    );
  };
  const observe = (kind: string, ev: any): Promise<any> | undefined => {
    const source = hostEvents;
    return source ? observeOutcome(() => source.observe(kind, ev)) : undefined;
  };
  // `turn_id` 的来源只有一个：**这个回合的身份**。实测（真 Pi 1.5.0，扩展侧事件）：
  //   · 助手消息上没有 `message.id` —— 它的键只有 api/content/model/provider/role/stopReason/timestamp/usage；
  //   · `messageEntryId` 只在 `turn_end` 出现，而工具在这之前就已经执行完（那时拿不到）。
  // 所以在 `turn_start`（工具之前）由宿主拼出：`pi-run-<pid>-<n>:<turnIndex>` —— 跨 run 不重数，
  // 同一回合的三件工具事件共用同一个值（runtime 定的 B 的语义：回合身份，不是重数的序号）。
  // 宿主没有某个事件 ⇒ 只少一个安全点，不让整条装配失败（12 处同一段 try/catch，收成一处）。
  const on = (name: any, fn: (event: any, ctx: any) => unknown) => {
    try { host.on(name, fn); } catch { /* 没有该事件 */ }
  };
  let runSeq = 0;
  let runId = '';
  on('agent_start', () => { runSeq += 1; runId = `pi-run-${process.pid}-${runSeq}`; currentTurnId = ''; });
  on('turn_start', (event: any) => {
    if (!runId) return;
    const idx = Number.isFinite(event?.turnIndex) ? event.turnIndex : 0;
    currentTurnId = `${runId}:${idx}`;
  });
  on('tool_execution_start', (event: any) => {
    const id = String(event?.toolCallId || '');
    if (!id || !currentTurnId || !hostEvents) return;
    const turnId = currentTurnId;
    toolsInFlight.set(id, {turnId, v: 0, last: '', started: observe('tool_start', {turnId, nativeId: id})});
  });
  on('tool_execution_update', (event: any) => {
    const id = String(event?.toolCallId || '');
    const tool = toolsInFlight.get(id);
    if (!tool || tool.outcome) return;
    let sig: string;
    try { sig = JSON.stringify(event?.partialResult ?? null); } catch { sig = String(event?.partialResult); }
    if (sig === tool.last) return;
    tool.last = sig;
    observe('tool_progress', {turnId: tool.turnId, nativeId: id, fields: {version: String(++tool.v)}});
  });
  const finishTool = (id: string, isError?: boolean) => {
    const tool = toolsInFlight.get(id);
    if (!tool) return;
    if (typeof isError === 'boolean') tool.outcome = isError ? 'error' : 'ok';
    if (!tool.outcome || tool.ending) return;
    // Keep the native result until Core acknowledges the call's original identity.
    tool.ending = (async () => {
      await tool.started;
      if (toolsInFlight.get(id) !== tool) return;
      const ack = await observe('tool_end', {turnId: tool.turnId, nativeId: id, fields: {outcome: tool.outcome}});
      if (ack?.ok && toolsInFlight.get(id) === tool) toolsInFlight.delete(id);
    })().finally(() => { tool.ending = undefined; });
  };
  on('tool_execution_end', (event: any) => {
    finishTool(String(event?.toolCallId || ''), Boolean(event?.isError));
  });
  on('message_end', ({message}: any) => {
    // The same finalized result Pi writes to its transcript, without waiting for the turn boundary.
    if (message?.role === 'toolResult') {
      finishTool(String(message.toolCallId || ''), Boolean(message.isError));
    }
  });
  // ★ W4：这一回合的**真实**原生用量。`turn_end.messageEntryId` 是本回合已持久那条 assistant
  //   entry 的 id（实测：`message_end` 时还没有 id，`turn_end` 才有），`getEntry(id)` 里那个 `usage`
  //   就是 provider 报的那一份 —— 不推算、不估费用、不猜岗位/Work。
  //   读不到 id / 读不到 entry / 没有 usage ⇒ **什么都不记**（没有数据不是“记一条 0”）。
  const reportUsage = (event: any, ctx: any) => {
    const entryId = String(event?.messageEntryId || '');
    if (!entryId) return;
    let msg: any = null;
    try {
      const got = ctx?.sessionManager?.getEntry?.(entryId);
      msg = (got && got.message) || got || null;
    } catch { return; }
    if (!msg || typeof msg !== 'object') return;
    const usage: any = msg.usage;
    if (!usage || typeof usage !== 'object') return;
    const count = (v: any) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.round(v) : undefined);
    const input = count(usage.input);
    const tokens = { in: input === undefined ? undefined : input + (count(usage.cacheWrite) ?? 0),
      out: count(usage.output), cache: count(usage.cacheRead) };
    if (!Object.values(tokens).some(Boolean)) return;
    const entry: any = { entry_id: entryId, tokens };
    const refs = new Set<string>();
    for (const result of Array.isArray(event?.toolResults) ? event.toolResults : []) {
      const key = result?.toolName === "office_work_publish" ? "sub_ref" : result?.toolName === "office_work_review" ? "review_ref" : "";
      const ref = key && result?.isError === false && result?.details?.ok === true && result.details[key];
      if (typeof ref === "string" && ref.trim()) refs.add(ref);
    }
    if (refs.size === 1) entry.ref = [...refs][0];
    if (typeof msg.model === 'string' && msg.model) entry.model = msg.model;
    // Pi 的 `timestamp` 是**毫秒**（pi-ai：请求开始时刻），Core 的 `at` 是秒。
    if (typeof msg.timestamp === 'number' && Number.isFinite(msg.timestamp)) entry.at = msg.timestamp / 1000;
    // 费用只在 provider **真的报了**的时候才带：`totalFromProvider` 就是那条凭据；0 不是免费。

    observeOutcome(() => wakeLoop?.reportUsage?.([entry], `usage:pi:${entryId}`));
  };
  on('turn_end', async (event: any, ctx: any) => {
    for (const result of event?.toolResults || []) {
      finishTool(String(result?.toolCallId || ''), Boolean(result?.isError));
    }
    // Retried only at Pi's existing boundary, with a completion actually observed.
    for (const id of toolsInFlight.keys()) finishTool(id);
    // 这一回合的门关了，且问出它关的是**哪一次尝试**，随 `turn_end` 一起报（Core 按它清心跳到期项）。
    // 实测 `turn_end.messageEntryId` 是本回合的真身份，但这条线上没有读者读 `turn_id`，所以不送。
    let closed: any = null;
    try { closed = wakeLoop?.turnClosed?.(currentTurnId); } catch { /* 观测不影响回合状态 */ }
    observe('turn_end', { turnId: currentTurnId, attemptId: closed && closed.attempt_id });
    reportUsage(event, ctx);
    if (!ctx.signal?.aborted && event.outcome === 'completed' && event.toolResults?.length && Array.isArray(event.entries)) {
      const draft = await wakeLoop?.insertAtToolBoundary(ctx);
      if (draft) return {entries: [...event.entries, draft], continue: true};
    }
  });
  // ★ W439 D2：模型产出的**唯一**原生事实是 assistant 的文本/思考增量（取证 `5e05476a`：
  //   `text_delta`/`thinking_delta` 只有 `{type, contentIndex, delta}`，没有任何 turn/run/增量 id）。
  //   所以这里不发明 id：关联用 wake loop 手里那次尝试的 `attempt_id`（与 `round.started` 同键）；
  //   节流与"回合已关就不再报"都在 wake loop 一处。工具块（`toolcall_*`）不算模型产出。
  on('message_update', (event: any) => {
    const t = String(event?.assistantMessageEvent?.type || '');
    if (t !== 'text_delta' && t !== 'thinking_delta') return;
    observeOutcome(() => wakeLoop?.modelProgress?.({ turnId: currentTurnId }));
  });
  on('before_agent_start', async (event: any, ctx: any) => {
    try { await wakeLoop?.beforeAgentStart?.(event, ctx); } catch { /* 不伪造 run association */ }
  });
  on('turn_start', async (_event: any, ctx: any) => {
    await wakeLoop?.confirmToolBoundary(ctx);
  });
  on('agent_start', async (_event: any, ctx: any) => {
    const attemptForStart = pendingAttemptKey;
    pendingAttemptKey = '';
    try { await wakeLoop?.agentStart?.(ctx, {pendingAttemptKey: attemptForStart}); }
    catch { /* Core 未确认则保留 unknown */ }
  });
  on('agent_settled', async (event: any, ctx: any) => {
    try { await wakeLoop?.agentSettled?.(event, ctx); } catch { /* 同上 */ }
  });
  // 端点选好之后起循环（`session_start` 时描述符可能还没写，故也挂在选端点之后）。
  on('session_start', (_e: any, ctx: any) => { try { startWakeLoop(ctx); } catch { /* 下一安全点再试 */ } });
  on('agent_settled', (_e: any, ctx: any) => { try { startWakeLoop(ctx); } catch { /* 同上 */ } });
  // ③ 关闭可退出：会话结束幂等关闭（文档要求"起长资源就必须注册 shutdown"）。
  on('session_shutdown', () => { stopWakeLoop(); });

  // Pi snapshots handlers for an event dispatch; handlers added by loadFull during the
  // current session_start cannot receive that same event. Start the already-bound session now.
  if (initialCtx) startWakeLoop(initialCtx);
  };

  // Unbound sessions expose only the shared join request; a verified endpoint enables the full surface.
  let boundUnsubs: Array<() => void> = [];
  const recordingPi = (): any => new Proxy(pi, {
    get(target: any, prop: string | symbol) {
      if (prop === "on") {
        return (event: string, handler: any) => {
          const off = target.on(event, handler);
          if (typeof off === "function") boundUnsubs.push(off);
          return off;
        };
      }
      const value = target[prop];
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  // 会话装配的代号：异步装载（动态 import）可能跨过下一次切换 —— 每个 await 之后
  // 都要核对代号，晚到的装载不许把工具面写到**新的**会话上（R3-W289.2 ②③ 的竞态）。
  let surfaceGen = 0;
  const loadFullTools = async (gen: number, ctx?: any): Promise<void> => {
    if (fullLoaded || gen !== surfaceGen) return;
    fullLoaded = true;
    try {
      const via = recordingPi();
      // 共享工具面：一张表（spec）+ 一条调用路径（office_tools），本宿主只负责登记与渲染。
      officePiTools(via, {scope: 'full'});
      wireBoundFeatures(via, ctx);
      dbg("load_ok");
    } catch (e) {
      fullLoaded = false;
    }
  };
  const loadFull = async (ctx: any, gen: number): Promise<void> => {
    // ★ R3-W289.2 ③：装工具面之前先确认端点仍是本会话的绑定 —— 否则工具会一直
    //   `no_controlled_endpoint`。本版不再去"选"端点，只核对宿主交付的那一份。
    try { keepDeliveredEndpoint(null, ctx); } catch { /* 核对不改变任何判定 */ }
    await loadFullTools(gen, ctx);
  };
  let fullLoaded = false;
  const joinOptions = { scope: "join" as const, home, session: (ctx: any) => ({
    ref: String(ctx?.sessionManager?.getSessionId?.() || ''), session_ref: nativeSessionFile(ctx),
  }) };
  const unloadFull = async (): Promise<void> => {
    const had = fullLoaded;
    fullLoaded = false;
    const offs = boundUnsubs; boundUnsubs = [];
    for (const off of offs) { try { off(); } catch { /* 已失效 */ } }
    dbg("unload_had=" + had);
    try {
      // 只撤**本扩展**加进去的业务工具，其余（其他插件的工具、MCP、用户关掉的内置）原样不动；
      // 未绑定会话保留加入申请入口。
      //   不能写成固定几件 —— 那会把别人的工具关掉、也会把用户有意关掉的内置重新打开（R3-W289.3 ①）。
      // ★ W436（2026-11-19）：“哪些名字是本扩展的工具”不再用**前缀猜**：那个答案在共享 spec 里
      //   （`TOOL_NAMES`），本宿主适配器注册时用的就是它。前缀写法会把**别人**注册的
      //   `office_*` 工具一起撤掉；清单写法只动自己的。
      const keep = pi.getActiveTools().filter((n: string) =>
        n === "office_request_create" || !TOOL_NAMES.includes(String(n)));
      pi.setActiveTools(keep);
      dbg("unload_set=" + keep.join(","));
    } catch { /* 工具面撤不掉时至少不留旧业务处理器 */ }
    officePiTools(pi, joinOptions);
    dbg("unload_done");
  };
  // 工具面诊断：面用**前缀**（它要报的是“此刻活着的、像 Office 的工具”，包括别人重复注册进来的那一个，
  // W289 那次泄面就是这样看出来的），撤销面的过滤器则精确到清单（见 `unloadFull`）。写走同一个 `wakeLog`。
  const dbg = (msg: string) => {
    let cur = "", names: string[] = [], tools: unknown = null;
    try {
      cur = String((pi as any).__w289CurSession || "");
      names = pi.getActiveTools().filter((n: string) => String(n).startsWith("office_"));
      tools = (pi as any).__officeToolStatus || null;
    } catch { /* 诊断不影响行为 */ }
    wakeLog(msg, {cur, fullLoaded, handlers: boundUnsubs.length, office: names, tools, endpoint: endpointNote || null});
  };
  // ★ W436（2026-11-19）：这里原来有个只读诊断钩子 `(pi as any).__officeDebugState`
  //   （`{fullLoaded, handlers, active}`）：产品代码、用例、证据脚本、文档都没有调用方，
  //   而同样这几个计数本来就经 `dbg()` 进 `W289_DEBUG`。删掉（`__w289CurSession` /
  //   `__officeToolNames` 留着：各有一个真读者 —— `dbg()`）。
  officePiTools(pi, joinOptions);
  const attachAtSafePoint = async (ctx: any) => {
    const gen = surfaceGen;
    if (!fullLoaded && await prepareSessionEndpoint(ctx, "", gen) && gen === surfaceGen) await loadFull(ctx, gen);
  };
  pi.on("before_agent_start", async (_event: any, ctx: any) => { await attachAtSafePoint(ctx); });
  const connectText = TOOL_TEXT._office_connect;
  pi.registerCommand("office-connect", {
    description: connectText.description,
    handler: async (args: string, ctx: any) => {
      const selected = args.trim();
      if (!selected) { ctx.ui.notify(connectText.usage, "warning"); return; }
      if (fullLoaded && resolve(selected) !== resolve(process.env.OFFICE_ENDPOINT_FILE || "")) {
        ctx.ui.notify(connectText.already_connected, "error"); return;
      }
      const gen = surfaceGen;
      try {
        if (!await prepareSessionEndpoint(ctx, selected, gen) || gen !== surfaceGen) {
          ctx.ui.notify(connectText.wrong_endpoint, "error"); return;
        }
        await loadFull(ctx, gen);
        ctx.ui.notify(fullLoaded ? connectText.connected : connectText.tools_failed, fullLoaded ? "info" : "error");
      } catch { ctx.ui.notify(connectText.unavailable, "error"); }
    },
  });
  // 已绑定的员工：正常启动时照旧拿到完整面（先判定再加载，不靠 env 冒充）。
  pi.on("session_start", async (_event: any, ctx: any) => {
    surfaceGen += 1; const gen = surfaceGen;
    try { (pi as any).__w289CurSession = nativeSessionFile(ctx); } catch { /* optional */ }
    try { await prepareSessionEndpoint(ctx, "", gen); } catch { /* keep the session unbound */ }
    if (gen !== surfaceGen) return;
    const v = endpointWhy(ctx);
    // 端点名可能刚被 `keepDeliveredEndpoint` 按"明确不匹配"撤掉：那一刻的原因在 `endpointNote` 里，
    // 别让后面这一读只报一个空名（"没有名字"和"名字是别人的"必须分得开）。
    const why = v.why === "no_delivered_endpoint" && endpointNote ? endpointNote : v.why;
    dbg("session_start bound=" + v.box + (why ? " why=" + why : ""));
    if (v.box) { await loadFull(ctx, gen); return; }
    // ★ 同一个 Pi 里从员工会话切到普通会话：旧完整面必须撤掉（工具与员工业务接线），
    //   否则普通会话继承上一段会话的 Office 能力 —— R3-W289 ②。
    await unloadFull();
  });
  // ★ R3-W289.2 ②：`session_before_switch` 是**可取消的事前事件** —— 别的扩展/用户可以取消它，
  //   在那上面撤销/装配工具会把"没发生的切换"当成事实。装配与撤销只挂在**成功之后**的
  //   `session_start`（实测：切换成功会派发 `session_start(resume)`，取消则不派发）。
  // 同一会话里刚接入成功：在宿主既有的 tool_result 安全点补齐，不需要用户 reload。
  pi.on("tool_result", async (event: any, ctx: any) => {
    if (!event || event.toolName !== "office_request_create" || event.isError) return;
    await attachAtSafePoint(ctx);
  });
  // 用户可能用宿主自己的 bash 跑 `postoffice add … --pi this`（真实新人就是这么接入的）：
  // `user_bash` 是宿主既有的安全点，接入成功后在同一次事件里补齐完整面（不轮询）。
  pi.on("user_bash", async (_event: any, ctx: any) => {
    if (fullLoaded) return;
    if (!boxForSession(ctx)) return;
    await loadFull(ctx, surfaceGen);
  });
}
