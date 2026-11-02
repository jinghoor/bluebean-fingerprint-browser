// lan-seats: 我的环境 as a native DSH sidebar panel.
//
// Measured from the shipped sidebar plugin: the sidebar builds one row per `sidebar.panellist` entry
// (`options.id`, `options.label`, `options.order`), and a row click calls `layout.selectPanel(id)`, which resolves to
// the **`main`** slot entry whose `key` is that id. A native panel therefore needs exactly two registrations:
//
//   1. `main`               keyed entry `key: 'lan-envs'`, rendering the environment list in the main area;
//   2. `sidebar.panellist`  the row itself: id `lan-envs`, label 我的环境, plus a small glyph.
//
// Nothing is shadowed or hidden: the DSH sidebar stays the browser's sidebar, and this is a plugin entry inside it,
// next to 插件 / 自动化任务 / 更多.
window.__ModuleLoader__.load({
  id: 'lan-seats',
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;

    const React = require('react');
    const h = React.createElement;

    const BASE = 'http://127.0.0.1:8799';
    const TOKEN = '__BLUEBEAN_BRIDGE_TOKEN_INJECTED_AT_RUNTIME__';
    const BLUEBEAN_VERSION = '1.0.4'; // Synchronized with bluebean_version.VERSION at runtime.

    function ProductInfo() {
      return h('div', { 'data-lan-product-info': '', style: { display: 'grid', gap: 5, fontSize: 12, lineHeight: '18px', userSelect: 'text' } },
        h('div', { style: { fontWeight: 600, color: 'var(--dsw-alias-text-primary,inherit)' } }, '蓝豆指纹浏览器 v' + BLUEBEAN_VERSION),
        h('div', { style: { color: 'var(--dsw-alias-text-secondary,#687386)', overflowWrap: 'anywhere' } }, '联系邮箱：277030704@qq.com'));
    }

    function installProductInfo(ctx) {
      // The account launcher owns a fixed menu with no additive slot. Add only
      // passive information to that menu; keep its native actions and focus order.
      ctx.effect(() => {
        let queued = 0;
        const decorate = () => {
          queued = 0;
          for (const menu of document.querySelectorAll('[role="menu"]')) {
            if (!menu.querySelector('[role="menuitem"][aria-keyshortcuts="Control+Alt+,"]') || menu.querySelector('[data-lan-product-menu-info]')) continue;
            const footer = document.createElement('div');
            footer.setAttribute('data-lan-product-menu-info', '');
            footer.style.cssText = 'display:grid;gap:5px;margin:6px 6px 2px;padding:10px 8px 6px;border-top:1px solid var(--dsw-alias-border-l2,rgba(128,128,128,.2));font-size:12px;line-height:18px;user-select:text;min-width:220px;max-width:calc(100vw - 60px);box-sizing:border-box';
            const title = document.createElement('div');
            title.textContent = '蓝豆指纹浏览器 v' + BLUEBEAN_VERSION;
            title.style.fontWeight = '600';
            const author = document.createElement('div');
            author.textContent = '联系邮箱：277030704@qq.com';
            author.style.cssText = 'color:var(--dsw-alias-text-secondary,#687386);overflow-wrap:anywhere';
            footer.append(title, author);
            (menu.querySelector('[role="presentation"]') || menu).append(footer);
          }
        };
        const observer = new MutationObserver(() => { if (!queued) queued = requestAnimationFrame(decorate); });
        // DSH portals this menu directly into body. Avoid observing chat/message
        // changes; only portal mount/unmount needs decoration.
        observer.observe(document.body, { childList: true });
        decorate();
        return () => {
          observer.disconnect();
          if (queued) cancelAnimationFrame(queued);
          document.querySelectorAll('[data-lan-product-menu-info]').forEach(node => node.remove());
        };
      }, 'lan-seats: product information in account settings menu');
      ctx.slots.inject('settings.general.item', () => ctx.slots.register(
        { name: 'settings.general.item', id: 'bluebean-product-info', order: 1000 }, ProductInfo));
    }

    async function call(path, body) {
      const response = await fetch(`${BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` },
        body: JSON.stringify(body ?? {}),
        signal: AbortSignal.timeout(path === MANAGE + 'status' ? 8000 : path === '/v1/lan/session-scope' ? 15000 : 30000),
      });
      return response.json().catch(() => ({ ok: false, error: 'bad json' }));
    }

    const MANAGE = '/v1/lan/manage/';
    // Reuse the management page's successful list read in the sidebar. Saving,
    // editing or removing an environment must not require toggling the sidebar
    // or starting another round of browser/proxy detection.
    const environmentCatalogEvents = new EventTarget();
    const UNKNOWN_STATUS={state:'unknown',label:'状态未知',network:'unchecked'};
    // A single lightweight poll is shared by both visible environment views.
    const runtimeStatus=(()=>{
      const listeners=new Set();let data={states:{}},timer=null,pending=null,version=0,failures=0,paused=0;
      const POLL_MS=15000;
      const emit=()=>listeners.forEach(fn=>fn(data));
      const schedule=()=>{clearTimeout(timer);if(!paused&&listeners.size&&!document.hidden){const activeAgent=Object.values(data.states||{}).some(s=>s?.state==='agent-running');timer=setTimeout(poll,activeAgent?2000:Math.min(30000,POLL_MS*(1+failures)));}};
      const poll=()=>{
        if(paused||!listeners.size||document.hidden)return;
        if(pending)return pending;
        const generation=version;
        pending=(async()=>{try{const reply=await call(MANAGE+'status');if(!reply.ok||!reply.states)throw Error('status unavailable');if(generation===version){data=reply;failures=0;emit();}}catch{if(generation===version){
          // Keep the last known per-environment state during a transient bridge
          // failure. Clearing states makes every indicator disappear and causes
          // a visible flicker while the next poll is already scheduled.
          data={...data,live_status:'offline'};failures++;emit();
        }}finally{pending=null;schedule();}})();
        return pending;
      };
      const visibility=()=>{clearTimeout(timer);if(!document.hidden)poll();};
      return {
        subscribe(fn){const first=!listeners.size;listeners.add(fn);fn(data);if(first){document.addEventListener('visibilitychange',visibility);poll();}return()=>{listeners.delete(fn);if(!listeners.size){clearTimeout(timer);document.removeEventListener('visibilitychange',visibility);}};},
        invalidate(){version++;clearTimeout(timer);if(pending)pending.finally(poll);else poll();},
        setActive(active,active_session=''){data={...data,active,active_session,live_status:data.live_status||'ready'};emit();},
        pause(){paused++;clearTimeout(timer);},
        resume(delay=1200){paused=Math.max(0,paused-1);if(!paused&&listeners.size&&!document.hidden){clearTimeout(timer);timer=setTimeout(poll,delay);}},
      };
    })();
    function statusCopy(s=UNKNOWN_STATUS){
      const active=s.agent_active_count||0,completed=s.agent_completed_count||0,failed=s.agent_failed_count||0;
      const warnings=[s.network_warning&&s.state!=='network-error'?'网络异常':'',s.agent_warning?(failed?failed+'项失败':'上次 Agent 操作失败'):''].filter(Boolean);
      const label=s.state==='agent-running'&&active>1?'Agent '+active+'项进行中':s.label||'状态未知';
      const text=[label,...warnings].join(' · ');
      const counts=active+completed+failed>1?'浏览器操作：'+active+' 项进行中，'+completed+' 项完成，'+failed+' 项失败':'';
      const detail=[text,counts,s.state==='unknown'?'暂时无法读取浏览器实时状态':'',s.state==='agent-complete'?'本次浏览器操作已完成；关闭环境后清除标记':'',s.running&&s.network==='unchecked'?'网络尚未检测':''].filter(Boolean).join('；');
      return {text,detail};
    }
    function RuntimeIndicator({status=UNKNOWN_STATUS}){
      const copy=statusCopy(status);
      return h('span',{className:'lan-runtime-indicator','data-state':status.state,title:copy.detail,'aria-label':copy.detail},h('i',{className:'lan-env-sidebar-dot','data-state':status.state,'aria-hidden':true}),h('span',{'data-warning':!!status.network_warning},copy.text));
    }
    // BEGIN VENDORED PLATFORM ICONS
    const PLATFORM_ICONS = {
      "facebook": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%230866FF%22%20d%3D%22M9.101%2023.691v-7.98H6.627v-3.667h2.474v-1.58c0-4.085%201.848-5.978%205.858-5.978.401%200%20.955.042%201.468.103a8.68%208.68%200%200%201%201.141.195v3.325a8.623%208.623%200%200%200-.653-.036%2026.805%2026.805%200%200%200-.733-.009c-.707%200-1.259.096-1.675.309a1.686%201.686%200%200%200-.679.622c-.258.42-.374.995-.374%201.752v1.297h3.919l-.386%202.103-.287%201.564h-3.246v8.245C19.396%2023.238%2024%2018.179%2024%2012.044c0-6.627-5.373-12-12-12s-12%205.373-12%2012c0%205.628%203.874%2010.35%209.101%2011.647Z%22%2F%3E%3C%2Fsvg%3E",
      "instagram": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23E4405F%22%20d%3D%22M7.0301.084c-1.2768.0602-2.1487.264-2.911.5634-.7888.3075-1.4575.72-2.1228%201.3877-.6652.6677-1.075%201.3368-1.3802%202.127-.2954.7638-.4956%201.6365-.552%202.914-.0564%201.2775-.0689%201.6882-.0626%204.947.0062%203.2586.0206%203.6671.0825%204.9473.061%201.2765.264%202.1482.5635%202.9107.308.7889.72%201.4573%201.388%202.1228.6679.6655%201.3365%201.0743%202.1285%201.38.7632.295%201.6361.4961%202.9134.552%201.2773.056%201.6884.069%204.9462.0627%203.2578-.0062%203.668-.0207%204.9478-.0814%201.28-.0607%202.147-.2652%202.9098-.5633.7889-.3086%201.4578-.72%202.1228-1.3881.665-.6682%201.0745-1.3378%201.3795-2.1284.2957-.7632.4966-1.636.552-2.9124.056-1.2809.0692-1.6898.063-4.948-.0063-3.2583-.021-3.6668-.0817-4.9465-.0607-1.2797-.264-2.1487-.5633-2.9117-.3084-.7889-.72-1.4568-1.3876-2.1228C21.2982%201.33%2020.628.9208%2019.8378.6165%2019.074.321%2018.2017.1197%2016.9244.0645%2015.6471.0093%2015.236-.005%2011.977.0014%208.718.0076%208.31.0215%207.0301.0839m.1402%2021.6932c-1.17-.0509-1.8053-.2453-2.2287-.408-.5606-.216-.96-.4771-1.3819-.895-.422-.4178-.6811-.8186-.9-1.378-.1644-.4234-.3624-1.058-.4171-2.228-.0595-1.2645-.072-1.6442-.079-4.848-.007-3.2037.0053-3.583.0607-4.848.05-1.169.2456-1.805.408-2.2282.216-.5613.4762-.96.895-1.3816.4188-.4217.8184-.6814%201.3783-.9003.423-.1651%201.0575-.3614%202.227-.4171%201.2655-.06%201.6447-.072%204.848-.079%203.2033-.007%203.5835.005%204.8495.0608%201.169.0508%201.8053.2445%202.228.408.5608.216.96.4754%201.3816.895.4217.4194.6816.8176.9005%201.3787.1653.4217.3617%201.056.4169%202.2263.0602%201.2655.0739%201.645.0796%204.848.0058%203.203-.0055%203.5834-.061%204.848-.051%201.17-.245%201.8055-.408%202.2294-.216.5604-.4763.96-.8954%201.3814-.419.4215-.8181.6811-1.3783.9-.4224.1649-1.0577.3617-2.2262.4174-1.2656.0595-1.6448.072-4.8493.079-3.2045.007-3.5825-.006-4.848-.0608M16.953%205.5864A1.44%201.44%200%201%200%2018.39%204.144a1.44%201.44%200%200%200-1.437%201.4424M5.8385%2012.012c.0067%203.4032%202.7706%206.1557%206.173%206.1493%203.4026-.0065%206.157-2.7701%206.1506-6.1733-.0065-3.4032-2.771-6.1565-6.174-6.1498-3.403.0067-6.156%202.771-6.1496%206.1738M8%2012.0077a4%204%200%201%201%204.008%203.9921A3.9996%203.9996%200%200%201%208%2012.0077%22%2F%3E%3C%2Fsvg%3E",
      "tiktok": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20256%20290%22%3E%3Cpath%20fill%3D%22%23ff004f%22%20d%3D%22M189.72%20104.421c18.678%2013.345%2041.56%2021.197%2066.273%2021.197v-47.53a67%2067%200%200%201-13.918-1.456v37.413c-24.711%200-47.59-7.851-66.272-21.195v96.996c0%2048.523-39.356%2087.855-87.9%2087.855c-18.113%200-34.949-5.473-48.934-14.86c15.962%2016.313%2038.222%2026.432%2062.848%2026.432c48.548%200%2087.905-39.332%2087.905-87.857v-96.995zm17.17-47.952c-9.546-10.423-15.814-23.893-17.17-38.785v-6.113h-13.189c3.32%2018.927%2014.644%2035.097%2030.358%2044.898M69.673%20225.607a40%2040%200%200%201-8.203-24.33c0-22.192%2018.001-40.186%2040.21-40.186a40.3%2040.3%200%200%201%2012.197%201.883v-48.593c-4.61-.631-9.262-.9-13.912-.801v37.822a40.3%2040.3%200%200%200-12.203-1.882c-22.208%200-40.208%2017.992-40.208%2040.187c0%2015.694%208.997%2029.281%2022.119%2035.9%22%2F%3E%3Cpath%20d%3D%22M175.803%2092.849c18.683%2013.344%2041.56%2021.195%2066.272%2021.195V76.631c-13.794-2.937-26.005-10.141-35.186-20.162c-15.715-9.802-27.038-25.972-30.358-44.898h-34.643v189.843c-.079%2022.132-18.049%2040.052-40.21%2040.052c-13.058%200-24.66-6.221-32.007-15.86c-13.12-6.618-22.118-20.206-22.118-35.898c0-22.193%2018-40.187%2040.208-40.187c4.255%200%208.356.662%2012.203%201.882v-37.822c-47.692.985-86.047%2039.933-86.047%2087.834c0%2023.912%209.551%2045.589%2025.053%2061.428c13.985%209.385%2030.82%2014.86%2048.934%2014.86c48.545%200%2087.9-39.335%2087.9-87.857z%22%2F%3E%3Cpath%20fill%3D%22%2300f2ea%22%20d%3D%22M242.075%2076.63V66.516a66.3%2066.3%200%200%201-35.186-10.047a66.47%2066.47%200%200%200%2035.186%2020.163M176.53%2011.57a68%2068%200%200%201-.728-5.457V0h-47.834v189.845c-.076%2022.13-18.046%2040.05-40.208%2040.05a40.06%2040.06%200%200%201-18.09-4.287c7.347%209.637%2018.949%2015.857%2032.007%2015.857c22.16%200%2040.132-17.918%2040.21-40.05V11.571zM99.966%20113.58v-10.769a89%2089%200%200%200-12.061-.818C39.355%20101.993%200%20141.327%200%20189.845c0%2030.419%2015.467%2057.227%2038.971%2072.996c-15.502-15.838-25.053-37.516-25.053-61.427c0-47.9%2038.354-86.848%2086.048-87.833%22%2F%3E%3C%2Fsvg%3E",
      "x": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23000000%22%20d%3D%22M18.901%201.153h3.68l-8.04%209.19L24%2022.846h-7.406l-5.8-7.584-6.638%207.584H.474l8.6-9.83L0%201.154h7.594l5.243%206.932ZM17.61%2020.644h2.039L6.486%203.24H4.298Z%22%2F%3E%3C%2Fsvg%3E",
      "youtube": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20256%20180%22%3E%3Cpath%20fill%3D%22red%22%20d%3D%22M250.346%2028.075A32.18%2032.18%200%200%200%20227.69%205.418C207.824%200%20127.87%200%20127.87%200S47.912.164%2028.046%205.582A32.18%2032.18%200%200%200%205.39%2028.24c-6.009%2035.298-8.34%2089.084.165%20122.97a32.18%2032.18%200%200%200%2022.656%2022.657c19.866%205.418%2099.822%205.418%2099.822%205.418s79.955%200%2099.82-5.418a32.18%2032.18%200%200%200%2022.657-22.657c6.338-35.348%208.291-89.1-.164-123.134%22%2F%3E%3Cpath%20fill%3D%22%23fff%22%20d%3D%22m102.421%20128.06l66.328-38.418l-66.328-38.418z%22%2F%3E%3C%2Fsvg%3E",
      "linkedin": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%230A66C2%22%20d%3D%22M20.447%2020.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853%200-2.136%201.445-2.136%202.939v5.667H9.351V9h3.414v1.561h.046c.477-.9%201.637-1.85%203.37-1.85%203.601%200%204.267%202.37%204.267%205.455v6.286zM5.337%207.433c-1.144%200-2.063-.926-2.063-2.065%200-1.138.92-2.063%202.063-2.063%201.14%200%202.064.925%202.064%202.063%200%201.139-.925%202.065-2.064%202.065zm1.782%2013.019H3.555V9h3.564v11.452zM22.225%200H1.771C.792%200%200%20.774%200%201.729v20.542C0%2023.227.792%2024%201.771%2024h20.451C23.2%2024%2024%2023.227%2024%2022.271V1.729C24%20.774%2023.2%200%2022.222%200h.003z%22%2F%3E%3C%2Fsvg%3E",
      "pinterest": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23BD081C%22%20d%3D%22M12.017%200C5.396%200%20.029%205.367.029%2011.987c0%205.079%203.158%209.417%207.618%2011.162-.105-.949-.199-2.403.041-3.439.219-.937%201.406-5.957%201.406-5.957s-.359-.72-.359-1.781c0-1.663.967-2.911%202.168-2.911%201.024%200%201.518.769%201.518%201.688%200%201.029-.653%202.567-.992%203.992-.285%201.193.6%202.165%201.775%202.165%202.128%200%203.768-2.245%203.768-5.487%200-2.861-2.063-4.869-5.008-4.869-3.41%200-5.409%202.562-5.409%205.199%200%201.033.394%202.143.889%202.741.099.12.112.225.085.345-.09.375-.293%201.199-.334%201.363-.053.225-.172.271-.401.165-1.495-.69-2.433-2.878-2.433-4.646%200-3.776%202.748-7.252%207.92-7.252%204.158%200%207.392%202.967%207.392%206.923%200%204.135-2.607%207.462-6.233%207.462-1.214%200-2.354-.629-2.758-1.379l-.749%202.848c-.269%201.045-1.004%202.352-1.498%203.146%201.123.345%202.306.535%203.55.535%206.607%200%2011.985-5.365%2011.985-11.987C23.97%205.39%2018.592.026%2011.985.026L12.017%200z%22%2F%3E%3C%2Fsvg%3E",
      "amazon": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23FF9900%22%20d%3D%22M.045%2018.02c.072-.116.187-.124.348-.022%203.636%202.11%207.594%203.166%2011.87%203.166%202.852%200%205.668-.533%208.447-1.595l.315-.14c.138-.06.234-.1.293-.13.226-.088.39-.046.525.13.12.174.09.336-.12.48-.256.19-.6.41-1.006.654-1.244.743-2.64%201.316-4.185%201.726a17.617%2017.617%200%2001-10.951-.577%2017.88%2017.88%200%2001-5.43-3.35c-.1-.074-.151-.15-.151-.22%200-.047.021-.09.051-.13zm6.565-6.218c0-1.005.247-1.863.743-2.577.495-.71%201.17-1.25%202.04-1.615.796-.335%201.756-.575%202.912-.72.39-.046%201.033-.103%201.92-.174v-.37c0-.93-.105-1.558-.3-1.875-.302-.43-.78-.65-1.44-.65h-.182c-.48.046-.896.196-1.246.46-.35.27-.575.63-.675%201.096-.06.3-.206.465-.435.51l-2.52-.315c-.248-.06-.372-.18-.372-.39%200-.046.007-.09.022-.15.247-1.29.855-2.25%201.82-2.88.976-.616%202.1-.975%203.39-1.05h.54c1.65%200%202.957.434%203.888%201.29.135.15.27.3.405.48.12.165.224.314.283.45.075.134.15.33.195.57.06.254.105.42.135.51.03.104.062.3.076.615.01.313.02.493.02.553v5.28c0%20.376.06.72.165%201.036.105.313.21.54.315.674l.51.674c.09.136.136.256.136.36%200%20.12-.06.226-.18.314-1.2%201.05-1.86%201.62-1.963%201.71-.165.135-.375.15-.63.045a6.062%206.062%200%2001-.526-.496l-.31-.347a9.391%209.391%200%2001-.317-.42l-.3-.435c-.81.886-1.603%201.44-2.4%201.665-.494.15-1.093.227-1.83.227-1.11%200-2.04-.343-2.76-1.034-.72-.69-1.08-1.665-1.08-2.94l-.05-.076zm3.753-.438c0%20.566.14%201.02.425%201.364.285.34.675.512%201.155.512.045%200%20.106-.007.195-.02.09-.016.134-.023.166-.023.614-.16%201.08-.553%201.424-1.178.165-.28.285-.58.36-.91.09-.32.12-.59.135-.8.015-.195.015-.54.015-1.005v-.54c-.84%200-1.484.06-1.92.18-1.275.36-1.92%201.17-1.92%202.43l-.035-.02zm9.162%207.027c.03-.06.075-.11.132-.17.362-.243.714-.41%201.05-.5a8.094%208.094%200%20011.612-.24c.14-.012.28%200%20.41.03.65.06%201.05.168%201.172.33.063.09.099.228.099.39v.15c0%20.51-.149%201.11-.424%201.8-.278.69-.664%201.248-1.156%201.68-.073.06-.14.09-.197.09-.03%200-.06%200-.09-.012-.09-.044-.107-.12-.064-.24.54-1.26.806-2.143.806-2.64%200-.15-.03-.27-.087-.344-.145-.166-.55-.257-1.224-.257-.243%200-.533.016-.87.046-.363.045-.7.09-1%20.135-.09%200-.148-.014-.18-.044-.03-.03-.036-.047-.02-.077%200-.017.006-.03.02-.063v-.06z%22%2F%3E%3C%2Fsvg%3E",
      "ebay": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23E53238%22%20d%3D%22M6.056%2012.132v-4.92h1.2v3.026c.59-.703%201.402-.906%202.202-.906%201.34%200%202.828.904%202.828%202.855%200%20.233-.015.457-.06.668.24-.953%201.274-1.305%202.896-1.344.51-.018%201.095-.018%201.56-.018v-.135c0-.885-.556-1.244-1.53-1.244-.72%200-1.245.3-1.305.81h-1.275c.136-1.29%201.5-1.62%202.686-1.62%201.064%200%201.995.27%202.415%201.02l-.436-.84h1.41l2.055%204.125%202.055-4.126H24l-3.72%207.305h-1.346l1.07-2.04-2.33-4.38c.13.255.2.555.2.93v2.46c0%20.346.01.69.04%201.005H16.8a6.543%206.543%200%2001-.046-.765c-.603.734-1.32.96-2.32.96-1.48%200-2.272-.78-2.272-1.695%200-.15.015-.284.037-.405-.3%201.246-1.36%202.086-2.767%202.086-.87%200-1.694-.315-2.2-.93%200%20.24-.015.494-.04.734h-1.18c.02-.39.04-.855.04-1.245v-1.05h-4.83c.065%201.095.818%201.74%201.853%201.74.718%200%201.355-.3%201.568-.93h1.24c-.24%201.29-1.61%201.725-2.79%201.725C.95%2015.009%200%2013.822%200%2012.232c0-1.754.982-2.91%203.116-2.91%201.688%200%202.93.886%202.94%202.806v.005zm9.137.183c-1.095.034-1.77.233-1.77.95%200%20.465.36.97%201.305.97%201.26%200%201.935-.69%201.935-1.814v-.13c-.45%200-.99.006-1.484.022h.012zm-6.06%201.875c1.11%200%201.876-.806%201.876-2.02s-.768-2.02-1.893-2.02c-1.11%200-1.89.806-1.89%202.02s.765%202.02%201.875%202.02h.03zm-4.35-2.514c-.044-1.125-.854-1.546-1.725-1.546-.944%200-1.694.474-1.815%201.546z%22%2F%3E%3C%2Fsvg%3E",
      "shopee": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23EE4D2D%22%20d%3D%22M15.9414%2017.9633c.229-1.879-.981-3.077-4.1758-4.0969-1.548-.528-2.277-1.22-2.26-2.1719.065-1.056%201.048-1.825%202.352-1.85a5.2898%205.2898%200%200%201%202.8838.89c.116.072.197.06.263-.039.09-.145.315-.494.39-.62.051-.081.061-.187-.068-.281-.185-.1369-.704-.4149-.983-.5319a6.4697%206.4697%200%200%200-2.5118-.514c-1.909.008-3.4129%201.215-3.5389%202.826-.082%201.1629.494%202.1078%201.73%202.8278.262.152%201.6799.716%202.2438.892%201.774.552%202.695%201.5419%202.478%202.6969-.197%201.047-1.299%201.7239-2.818%201.7439-1.2039-.046-2.2878-.537-3.1278-1.19l-.141-.11c-.104-.08-.218-.075-.287.03-.05.077-.376.547-.458.67-.077.108-.035.168.045.234.35.293.817.613%201.134.775a6.7097%206.7097%200%200%200%202.8289.727%204.9048%204.9048%200%200%200%202.0759-.354c1.095-.465%201.8029-1.394%201.9449-2.554zM11.9986%201.4009c-2.068%200-3.7539%201.95-3.8329%204.3899h7.6657c-.08-2.44-1.765-4.3899-3.8328-4.3899zm7.8516%2022.5981-.08.001-15.7843-.002c-1.074-.04-1.863-.91-1.971-1.991l-.01-.195L1.298%206.2858a.459.459%200%200%201%20.45-.494h4.9748C6.8448%202.568%209.1607%200%2011.9996%200c2.8388%200%205.1537%202.5689%205.2757%205.7898h4.9678a.459.459%200%200%201%20.458.483l-.773%2015.5883-.007.131c-.094%201.094-.979%201.9769-2.0709%202.0059z%22%2F%3E%3C%2Fsvg%3E",
      "lazada": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2048%2048%22%20stroke-width%3D%222.4%22%3E%3Cpath%20fill%3D%22none%22%20stroke%3D%22%23F57224%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%20d%3D%22m34.382%206.28l-6.426%203.71c-2.182%201.26-5.732%201.254-7.928-.014L13.56%206.242L4.5%2011.473l19.56%2011.294L43.5%2011.544z%22%2F%3E%3Cpath%20fill%3D%22none%22%20stroke%3D%22%23F57224%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%20d%3D%22M43.5%2011.544v19.27L24.966%2041.517a1.81%201.81%200%200%201-1.81%200L4.5%2030.745V11.473m19.561%2011.294v18.987%22%2F%3E%3C%2Fsvg%3E",
      "etsy": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23F16521%22%20d%3D%22M8.559%202.445c0-.325.033-.52.59-.52h7.465c1.3%200%202.02%201.11%202.54%203.193l.42%201.666h1.27c.23-4.728.43-6.784.43-6.784s-3.196.36-5.09.36H6.635L1.521.196v1.37l1.725.326c1.21.24%201.5.496%201.6%201.606%200%200%20.11%203.27.11%208.64%200%205.385-.09%208.61-.09%208.61%200%20.973-.39%201.333-1.59%201.573l-1.722.33V24l5.13-.165h8.55c1.935%200%206.39.165%206.39.165.105-1.17.75-6.48.855-7.064h-1.2l-1.284%202.91c-1.005%202.28-2.476%202.445-4.11%202.445h-4.906c-1.63%200-2.415-.64-2.415-2.05V12.8s3.62%200%204.79.096c.912.064%201.463.325%201.76%201.598l.39%201.695h1.41l-.09-4.278.192-4.305h-1.391l-.45%201.89c-.283%201.244-.48%201.47-1.754%201.6-1.666.17-4.815.14-4.815.14V2.45h-.05z%22%2F%3E%3C%2Fsvg%3E",
      "aliexpress": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%23FF4747%22%20d%3D%22M5.166%209.096a.022.022%200%200%200-.022.021c0%20.396-.32.717-.713.717a.021.021%200%200%200-.021.022c0%20.012.01.021.021.021.394%200%20.713.322.713.718%200%20.012.01.021.022.021.011%200%20.021-.01.021-.021A.717.717%200%200%201%205.9%209.88a.021.021%200%200%200%200-.043.716.716%200%200%201-.713-.718v-.002a.021.021%200%200%200-.006-.015.022.022%200%200%200-.015-.006zm-3.693.526L0%2013.462h.48l.355-.922h1.782l.354.922h.481L1.98%209.622zm2.264.002v3.838h.491V9.624zm2.375%200v3.838h2.413v-.502H6.613v-1.19H8.19v-.477H6.613v-1.166h1.773v-.502zm-4.386.592l.698%201.82H1.028zm14.689.402a1.466%201.466%200%200%200-.966.366V10.7h-.491v2.763h.49c.002-.477%200-.955.002-1.433a.969.969%200%200%201%20.965-.918zm4.18.007c-.053%200-.105.003-.158.01-.315.031-.606.175-.753.377a.689.689%200%200%200-.14.465c.007.2.066.357.233.496.184.147.42.2.657.259.311.067.426.095.546.186.08.07.133.127.136.27%200%20.25-.221.372-.42.41a.89.89%200%200%201-.894-.344l-.371.288c.33.382.777.505%201.09.5.54-.01.891-.217%201.029-.534.066-.153.063-.309.063-.38a.677.677%200%200%200-.267-.545c-.228-.177-.583-.228-.636-.242-.437-.078-.658-.196-.697-.341-.043-.192.102-.35.297-.411a.76.76%200%200%201%20.857.277l.367-.247a1.166%201.166%200%200%200-.939-.494zm2.387%200c-.052%200-.105.003-.157.01-.316.031-.607.175-.753.377a.689.689%200%200%200-.14.465c.006.2.065.357.233.496.183.147.42.2.657.259.31.067.426.095.545.186.081.07.134.127.136.27.001.25-.221.372-.42.41a.89.89%200%200%201-.894-.344l-.371.288c.33.382.777.505%201.09.5.541-.01.891-.217%201.03-.534.065-.153.062-.309.062-.38a.677.677%200%200%200-.267-.545c-.227-.177-.583-.228-.636-.242-.437-.078-.658-.196-.696-.341-.043-.192.101-.35.297-.411a.76.76%200%200%201%20.857.277l.367-.247a1.167%201.167%200%200%200-.94-.494zm-9.84.002a1.461%201.461%200%200%200-1.42%201.117%201.305%201.305%200%200%200-.041.327v2.833h.491v-1.813c.17.18.487.42.96.454a1.447%201.447%200%200%200%201.208-.627%201.457%201.457%200%200%200-1.199-2.292zm4.804%200a1.448%201.448%200%200%200-1.288%202.08c.255.53.811.87%201.412.833a1.452%201.452%200%200%200%201.012-.51l-.363-.291a.968.968%200%200%201-1.106.273%201.01%201.01%200%200%201-.602-.69h2.239l.002-.427a1.295%201.295%200%200%200-1.306-1.268zm-9.2.08l1.062%201.377-1.062%201.378h.581l.779-1.01.778%201.01h.581l-1.062-1.378%201.062-1.378h-.581l-.778%201.01-.779-1.01zm-3.825.015v2.74h.49v-2.74zm8.233.37a.96.96%200%200%201%20.95.993.963.963%200%200%201-.863.998.962.962%200%200%201-1.034-.739c-.074-.382%200-.746.307-1.019a.959.959%200%200%201%20.64-.233zm4.79.015a.823.823%200%200%201%20.819.755h-1.76a.964.964%200%200%201%20.94-.755z%22%2F%3E%3C%2Fsvg%3E",
      "shopify": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%3E%3Cpath%20fill%3D%22%237AB55C%22%20d%3D%22M15.337%2023.979l7.216-1.561s-2.604-17.613-2.625-17.73c-.018-.116-.114-.192-.211-.192s-1.929-.136-1.929-.136-1.275-1.274-1.439-1.411c-.045-.037-.075-.057-.121-.074l-.914%2021.104h.023zM11.71%2011.305s-.81-.424-1.774-.424c-1.447%200-1.504.906-1.504%201.141%200%201.232%203.24%201.715%203.24%204.629%200%202.295-1.44%203.76-3.406%203.76-2.354%200-3.54-1.465-3.54-1.465l.646-2.086s1.245%201.066%202.28%201.066c.675%200%20.975-.545.975-.932%200-1.619-2.654-1.694-2.654-4.359-.034-2.237%201.571-4.416%204.827-4.416%201.257%200%201.875.361%201.875.361l-.945%202.715-.02.01zM11.17.83c.136%200%20.271.038.405.135-.984.465-2.064%201.639-2.508%203.992-.656.213-1.293.405-1.889.578C7.697%203.75%208.951.84%2011.17.84V.83zm1.235%202.949v.135c-.754.232-1.583.484-2.394.736.466-1.777%201.333-2.645%202.085-2.971.193.501.309%201.176.309%202.1zm.539-2.234c.694.074%201.141.867%201.429%201.755-.349.114-.735.231-1.158.366v-.252c0-.752-.096-1.371-.271-1.871v.002zm2.992%201.289c-.02%200-.06.021-.078.021s-.289.075-.714.21c-.423-1.233-1.176-2.37-2.508-2.37h-.115C12.135.209%2011.669%200%2011.265%200%208.159%200%206.675%203.877%206.21%205.846c-1.194.365-2.063.636-2.16.674-.675.213-.694.232-.772.87-.075.462-1.83%2014.063-1.83%2014.063L15.009%2024l.927-21.166z%22%2F%3E%3C%2Fsvg%3E",
      "google": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20256%20262%22%3E%3Cpath%20fill%3D%22%234285f4%22%20d%3D%22M255.878%20133.451c0-10.734-.871-18.567-2.756-26.69H130.55v48.448h71.947c-1.45%2012.04-9.283%2030.172-26.69%2042.356l-.244%201.622l38.755%2030.023l2.685.268c24.659-22.774%2038.875-56.282%2038.875-96.027%22%2F%3E%3Cpath%20fill%3D%22%2334a853%22%20d%3D%22M130.55%20261.1c35.248%200%2064.839-11.605%2086.453-31.622l-41.196-31.913c-11.024%207.688-25.82%2013.055-45.257%2013.055c-34.523%200-63.824-22.773-74.269-54.25l-1.531.13l-40.298%2031.187l-.527%201.465C35.393%20231.798%2079.49%20261.1%20130.55%20261.1%22%2F%3E%3Cpath%20fill%3D%22%23fbbc05%22%20d%3D%22M56.281%20156.37c-2.756-8.123-4.351-16.827-4.351-25.82c0-8.994%201.595-17.697%204.206-25.82l-.073-1.73L15.26%2071.312l-1.335.635C5.077%2089.644%200%20109.517%200%20130.55s5.077%2040.905%2013.925%2058.602z%22%2F%3E%3Cpath%20fill%3D%22%23eb4335%22%20d%3D%22M130.55%2050.479c24.514%200%2041.05%2010.589%2050.479%2019.438l36.844-35.974C195.245%2012.91%20165.798%200%20130.55%200C79.49%200%2035.393%2029.301%2013.925%2071.947l42.211%2032.783c10.59-31.477%2039.891-54.251%2074.414-54.251%22%2F%3E%3C%2Fsvg%3E",
      "microsoft": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20256%20256%22%3E%3Cpath%20fill%3D%22%23f1511b%22%20d%3D%22M121.666%20121.666H0V0h121.666z%22%2F%3E%3Cpath%20fill%3D%22%2380cc28%22%20d%3D%22M256%20121.666H134.335V0H256z%22%2F%3E%3Cpath%20fill%3D%22%2300adef%22%20d%3D%22M121.663%20256.002H0V134.336h121.663z%22%2F%3E%3Cpath%20fill%3D%22%23fbbc09%22%20d%3D%22M256%20256.002H134.335V134.336H256z%22%2F%3E%3C%2Fsvg%3E",
      "tiktok-shop": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%20256%20290%22%3E%3Cpath%20fill%3D%22%23ff004f%22%20d%3D%22M189.72%20104.421c18.678%2013.345%2041.56%2021.197%2066.273%2021.197v-47.53a67%2067%200%200%201-13.918-1.456v37.413c-24.711%200-47.59-7.851-66.272-21.195v96.996c0%2048.523-39.356%2087.855-87.9%2087.855c-18.113%200-34.949-5.473-48.934-14.86c15.962%2016.313%2038.222%2026.432%2062.848%2026.432c48.548%200%2087.905-39.332%2087.905-87.857v-96.995zm17.17-47.952c-9.546-10.423-15.814-23.893-17.17-38.785v-6.113h-13.189c3.32%2018.927%2014.644%2035.097%2030.358%2044.898M69.673%20225.607a40%2040%200%200%201-8.203-24.33c0-22.192%2018.001-40.186%2040.21-40.186a40.3%2040.3%200%200%201%2012.197%201.883v-48.593c-4.61-.631-9.262-.9-13.912-.801v37.822a40.3%2040.3%200%200%200-12.203-1.882c-22.208%200-40.208%2017.992-40.208%2040.187c0%2015.694%208.997%2029.281%2022.119%2035.9%22%2F%3E%3Cpath%20d%3D%22M175.803%2092.849c18.683%2013.344%2041.56%2021.195%2066.272%2021.195V76.631c-13.794-2.937-26.005-10.141-35.186-20.162c-15.715-9.802-27.038-25.972-30.358-44.898h-34.643v189.843c-.079%2022.132-18.049%2040.052-40.21%2040.052c-13.058%200-24.66-6.221-32.007-15.86c-13.12-6.618-22.118-20.206-22.118-35.898c0-22.193%2018-40.187%2040.208-40.187c4.255%200%208.356.662%2012.203%201.882v-37.822c-47.692.985-86.047%2039.933-86.047%2087.834c0%2023.912%209.551%2045.589%2025.053%2061.428c13.985%209.385%2030.82%2014.86%2048.934%2014.86c48.545%200%2087.9-39.335%2087.9-87.857z%22%2F%3E%3Cpath%20fill%3D%22%2300f2ea%22%20d%3D%22M242.075%2076.63V66.516a66.3%2066.3%200%200%201-35.186-10.047a66.47%2066.47%200%200%200%2035.186%2020.163M176.53%2011.57a68%2068%200%200%201-.728-5.457V0h-47.834v189.845c-.076%2022.13-18.046%2040.05-40.208%2040.05a40.06%2040.06%200%200%201-18.09-4.287c7.347%209.637%2018.949%2015.857%2032.007%2015.857c22.16%200%2040.132-17.918%2040.21-40.05V11.571zM99.966%20113.58v-10.769a89%2089%200%200%200-12.061-.818C39.355%20101.993%200%20141.327%200%20189.845c0%2030.419%2015.467%2057.227%2038.971%2072.996c-15.502-15.838-25.053-37.516-25.053-61.427c0-47.9%2038.354-86.848%2086.048-87.833%22%2F%3E%3C%2Fsvg%3E",
      "environment": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22%23687D9B%22%20stroke-width%3D%221.5%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Crect%20x%3D%223%22%20y%3D%224%22%20width%3D%2218%22%20height%3D%2216%22%20rx%3D%223%22%2F%3E%3Cpath%20d%3D%22M3%209h18M7%206.5h.01M10%206.5h.01%22%2F%3E%3Ccircle%20cx%3D%2212%22%20cy%3D%2214.5%22%20r%3D%223%22%2F%3E%3Cpath%20d%3D%22M9%2014.5h6M12%2011.5v6%22%2F%3E%3C%2Fsvg%3E",
      "custom": "data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22%23687D9B%22%20stroke-width%3D%221.5%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Ccircle%20cx%3D%2212%22%20cy%3D%2212%22%20r%3D%229%22%2F%3E%3Cellipse%20cx%3D%2212%22%20cy%3D%2212%22%20rx%3D%224%22%20ry%3D%229%22%2F%3E%3Cpath%20d%3D%22M3%2012h18M5%207h14M5%2017h14%22%2F%3E%3C%2Fsvg%3E"
    };
    // END VENDORED PLATFORM ICONS

    function accountIconKey(account={}) {
      const id=String(account.platform_id||'').toLowerCase();
      if(id!=='custom'&&PLATFORM_ICONS[id])return id;
      const name=String(account.platform||'').toLowerCase().replace(/[\s_-]+/g,'');
      const aliases={facebook:'facebook',instagram:'instagram',tiktok:'tiktok',tiktokshop:'tiktok-shop',twitter:'x',x:'x',youtube:'youtube',linkedin:'linkedin',pinterest:'pinterest',amazon:'amazon',ebay:'ebay',shopee:'shopee',lazada:'lazada',etsy:'etsy',aliexpress:'aliexpress',shopify:'shopify',google:'google',microsoft:'microsoft'};
      if(aliases[name])return aliases[name];
      try {
        const host=new URL(account.url).hostname.toLowerCase();
        const domains={'facebook.com':'facebook','instagram.com':'instagram','tiktok.com':'tiktok','tiktokglobalshop.com':'tiktok-shop','x.com':'x','twitter.com':'x','youtube.com':'youtube','linkedin.com':'linkedin','pinterest.com':'pinterest','amazon.com':'amazon','ebay.com':'ebay','shopee.com':'shopee','lazada.com':'lazada','etsy.com':'etsy','aliexpress.com':'aliexpress','shopify.com':'shopify','google.com':'google','live.com':'microsoft','microsoft.com':'microsoft'};
        for(const [domain,key] of Object.entries(domains))if(host===domain||host.endsWith('.'+domain))return key;
      } catch { /* Custom and legacy accounts can have no website URL. */ }
      return 'custom';
    }
    function iconProps(key,size=18) {
      return {src:PLATFORM_ICONS[key]||PLATFORM_ICONS.custom,alt:'','aria-hidden':true,draggable:false,width:size,height:size,'data-platform-icon':key,
        style:{display:'inline-block',width:size,height:size,objectFit:'contain',flexShrink:0,verticalAlign:'middle',background:key==='environment'?'transparent':'#fff',padding:key==='environment'?0:2,borderRadius:4}};
    }
    const platformIcon=(account,size=18)=>h('img',iconProps(accountIconKey(account),size));
    const environmentIcon=(size=18)=>h('img',iconProps('environment',size));
    function appendIcon(parent,key,size=18) {
      const img=document.createElement('img'),props=iconProps(key,size);
      img.src=props.src;img.alt='';img.setAttribute('aria-hidden','true');img.draggable=false;img.width=img.height=size;img.dataset.platformIcon=key;
      Object.assign(img.style,{...props.style,width:size+'px',height:size+'px',padding:props.style.padding+'px',borderRadius:'4px'});
      parent.append(img);
      return img;
    }
    const CONFIG_FIELDS = ['env_id', 'display_name', 'group', 'tags', 'notes', 'proxy', 'expected_exit_ip',
      'locale', 'accept_languages', 'fingerprint_seed', 'start_url', 'homepage', 'ip_mode'];
    const LABELS = { env_id: '环境编号', display_name: '环境名称', group: '分组', tags: '标签', notes: '备注',
      ip_mode: 'IP 模式', proxy: '代理地址', expected_exit_ip: '预期出口 IP', locale: '区域设置', accept_languages: '请求语言',
      fingerprint_seed: '指纹种子', start_url: '首次启动页面', homepage: '新标签页主页',
      platform: '平台/站点', label: '账号备注', username: '账号名称', password: '更新密码', otp_secret: '更新 OTP 密钥' };
    const emptyAccount = () => ({ platform: '', label: '', username: '', password: '', otp_secret: '' });
    const readJobId = () => { try { return sessionStorage.getItem('lan-env-proxy-job'); } catch { return null; } };
    const rememberJob = job => {
      try { if (job) sessionStorage.setItem('lan-env-proxy-job', job.id); else sessionStorage.removeItem('lan-env-proxy-job'); } catch { /* Storage is optional; the service retains task progress. */ }
    };
    const viewDefaults = { query: '', group: '', tag: '', status: '', sort: 'name' };
    const readView = () => {
      try {
        const value = JSON.parse(sessionStorage.getItem('lan-env-list-view') || '{}');
        const restored = Object.fromEntries(Object.entries(viewDefaults).map(([key, fallback]) =>
          [key, typeof value?.[key] === 'string' ? value[key].slice(0, 300) : fallback]));
        if (!['', 'active', 'account', 'failed', 'untested'].includes(restored.status)) restored.status = '';
        if (!['name', 'updated', 'recent'].includes(restored.sort)) restored.sort = 'name';
        return restored;
      } catch { return { ...viewDefaults }; }
    };
    const configOf = row => Object.fromEntries(CONFIG_FIELDS.map(key => [key, row?.[key] || (key==='ip_mode'?'static':'')]));
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const dateLabel = seconds => seconds ? new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
    const proxyStatus = env => ['', 'direct', 'system'].includes(env.proxy) ? '无需检测' :
      env.proxy?.startsWith('socks') ? '暂不支持检测' : env.proxy_test?.stale ? '待重新检测' :
      ({ success: '连接正常', failed: '连接失败', mismatch: '出口与预期不符', unsupported: '暂不支持检测', not_required: '无需检测' }[env.proxy_test?.status] || '未检测');
    const CSS = `
      .lan-manager { --lm-border:var(--dsw-alias-border-l2,#d9dde5); --lm-blue:#2367df; --lm-muted:var(--dsw-alias-text-secondary,#586174);
        --lm-bg:var(--dsw-alias-background-primary,#fff); color:inherit; font-size:13px; line-height:1.55; padding:20px; min-width:0; max-width:1040px; box-sizing:border-box; container:lm-panel / inline-size; }
      .lan-manager * { box-sizing:border-box; }
      .lan-manager button,.lan-manager input,.lan-manager select,.lan-manager textarea { font:inherit; color:inherit; }
      .lan-manager button { border:1px solid var(--lm-border); background:transparent; border-radius:7px; padding:7px 11px; cursor:pointer; transition:background .14s, border-color .14s; }
      .lan-manager button:hover { background:rgba(90,120,170,.08); }
      .lan-manager button:focus-visible,.lan-manager input:focus-visible,.lan-manager select:focus-visible,.lan-manager textarea:focus-visible,.lan-manager summary:focus-visible { outline:2px solid var(--lm-blue); outline-offset:2px; }
      .lan-manager button:disabled { opacity:.55; cursor:wait; }
      .lan-manager .lm-primary { color:#fff; background:var(--lm-blue); border-color:var(--lm-blue); }
      .lan-manager .lm-primary:hover { background:#1d56bc; }
      .lan-manager .lm-danger { color:#b42318; }
      .lan-manager h2 { margin:0; font-size:20px; font-weight:650; } .lan-manager h3 { margin:0 0 12px; font-size:15px; }
      .lan-manager p { margin:6px 0 12px; } .lan-manager small,.lan-manager .lm-muted { color:var(--lm-muted); font-size:13px; }
      .lan-manager .lm-head,.lan-manager .lm-actions { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
      .lan-manager .lm-actions { justify-content:flex-start; } .lan-manager .lm-head { margin-bottom:16px; }
      .lan-manager .lm-tabs { display:flex; gap:6px; margin:12px 0 16px; border-bottom:1px solid var(--lm-border); padding-bottom:10px; }
      .lan-manager .lm-tabs button[aria-selected=true] { color:var(--lm-blue); border-color:var(--lm-blue); background:rgba(35,103,223,.06); }
      .lan-manager .lm-filters { display:grid; grid-template-columns:minmax(160px,2fr) repeat(3,minmax(95px,1fr)); gap:10px; margin-bottom:12px; }
      .lan-manager input:not([type=checkbox]),.lan-manager textarea,.lan-manager select { border:1px solid var(--lm-border); border-radius:7px; background:transparent; padding:8px 10px; width:100%; min-width:0; }
      .lan-manager input[aria-invalid=true] { border-color:#b42318; }
      .lan-manager label.lm-field { display:grid; gap:5px; min-width:0; }
      .lan-manager label.lm-check { display:flex; align-items:center; gap:7px; }
      .lan-manager .lm-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px 18px; }
      .lan-manager .lm-wide { grid-column:1/-1; }
      .lan-manager .lm-section { border-top:1px solid var(--lm-border); padding:18px 0; }
      .lan-manager .lm-section > summary { font-size:15px; font-weight:600; cursor:pointer; margin-bottom:12px; }
      .lan-manager .lm-error { color:#b42318; font-size:13px; }
      .lan-manager .lm-notice { border:1px solid var(--lm-border); padding:10px 12px; border-radius:7px; margin-bottom:12px; overflow-wrap:anywhere; }
      .lan-manager .lm-list { border-top:1px solid var(--lm-border); }
      .lan-manager .lm-row { display:grid; grid-template-columns:20px minmax(100px,1.6fr) minmax(100px,1fr) auto; gap:12px; align-items:center; border-bottom:1px solid var(--lm-border); padding:14px 0; }
      .lan-manager .lm-row[data-active=true] .lm-name { color:var(--lm-blue); }
      .lan-manager .lm-name { font-weight:600; overflow-wrap:anywhere; } .lan-manager .lm-identity { min-width:0; }
      .lan-manager .lm-identity small { display:block; overflow-wrap:anywhere; }
      .lan-manager .lm-footer { display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; align-items:center; margin-top:16px; }
      .lan-manager .lm-savebar { position:sticky; bottom:0; background:var(--lm-bg); padding:14px 0 6px; border-top:1px solid var(--lm-border); z-index:2; }
      .lan-manager dl { display:grid; grid-template-columns:120px minmax(0,1fr); gap:6px 14px; margin:12px 0; } .lan-manager dt { color:var(--lm-muted); } .lan-manager dd { margin:0; overflow-wrap:anywhere; }
      .lan-manager .lm-fingerprint-summary { display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:10px; border:1px solid var(--lm-border); border-radius:7px; padding:10px 12px; margin:12px 0 2px; background:rgba(35,103,223,.045); }
      .lan-manager .lm-fingerprint-summary strong { color:var(--lm-blue); white-space:nowrap; }
      .lan-manager .lm-fingerprint-summary span { min-width:0; overflow-wrap:anywhere; }
      .lan-manager .lm-fingerprint-summary small { text-align:right; white-space:nowrap; }
      .lan-manager .lm-fingerprint-block { padding:14px 0; }
      .lan-manager .lm-fingerprint-block h3 { margin-bottom:8px; }
      .lan-manager .lm-fingerprint-privacy { border-top:1px solid var(--lm-border); padding-top:12px; }
      .lan-manager .lm-progress { width:100%; height:8px; accent-color:var(--lm-blue); }
      .lan-manager .lm-result { border-bottom:1px solid var(--lm-border); padding:9px 0; overflow-wrap:anywhere; }
      .lan-manager .lm-enter { animation:lm-enter .16s ease-out; } @keyframes lm-enter { from { opacity:.4; transform:translateY(3px) } to { opacity:1; transform:none } }
      .lan-manager .lm-just-saved { animation:lm-saved 1.6s ease-out; } @keyframes lm-saved { from { background:rgba(35,103,223,.15) } to { background:transparent } }
      @media(max-width:700px) { .lan-manager { padding:16px; } .lan-manager .lm-filters { grid-template-columns:repeat(2,minmax(0,1fr)); } .lan-manager .lm-row { grid-template-columns:18px minmax(0,1fr) minmax(100px,.65fr) auto; gap:6px; align-items:center; padding:2px 0; min-height:36px; } .lan-manager .lm-status { grid-column:auto; grid-row:auto; min-width:0; overflow:hidden; text-overflow:ellipsis; } .lan-manager .lm-row-actions { grid-column:auto; grid-row:auto; flex-direction:row; gap:4px; } .lan-manager .lm-row-actions button { padding:3px 6px; min-height:26px; font-size:11px; } }
      @media(max-width:440px) { .lan-manager .lm-grid { grid-template-columns:1fr; } .lan-manager dl { grid-template-columns:90px minmax(0,1fr); } }
      @media(max-width:600px) { .lan-manager .lm-fingerprint-summary { grid-template-columns:1fr; gap:4px; } .lan-manager .lm-fingerprint-summary small { text-align:left; } }
      @container lm-panel (max-width:700px) { .lm-filters { grid-template-columns:repeat(2,minmax(0,1fr)) !important; } .lan-manager .lm-row { grid-template-columns:18px minmax(0,1fr) minmax(100px,.65fr) auto !important; gap:6px; align-items:center; padding:2px 0; min-height:36px; } .lan-manager .lm-status { grid-column:auto; grid-row:auto; min-width:0; overflow:hidden; text-overflow:ellipsis; } .lan-manager .lm-row-actions { grid-column:auto; grid-row:auto; flex-direction:row; gap:4px; } .lan-manager .lm-row-actions button { padding:3px 6px; min-height:26px; font-size:11px; } }
      @container lm-panel (max-width:460px) { .lm-grid { grid-template-columns:1fr !important; } .lan-manager dl { grid-template-columns:90px minmax(0,1fr); } }
      @media(prefers-reduced-motion:reduce) { .lan-manager .lm-enter,.lan-manager .lm-just-saved { animation:none; } .lan-manager button { transition:none; } }
    `;

    function useEnvs() {
      const [state, setState] = React.useState({ envs: [], active: '', error: '', loading: true, api_version: 0 });
      const inFlight = React.useRef(null);
      const live = React.useRef(true);
      const refresh = React.useCallback(async (fresh = false) => {
        if (fresh === true && inFlight.current) await inFlight.current;
        if (inFlight.current) return inFlight.current;
        inFlight.current = (async () => {
          try {
            const reply = await call(MANAGE + 'list');
            if (!reply.ok) throw new Error(reply.error || '连接失败');
            if (reply.api_version !== 2) throw new Error('管理服务版本不兼容，请升级本地桥接服务');
            if (live.current) {
              setState({ ...reply, error: '', loading: false });
              environmentCatalogEvents.dispatchEvent(new CustomEvent('updated', { detail: reply }));
            }
            return reply;
          } catch (err) {
            if (live.current) setState(old => ({ ...old, loading: false, error: String(err.message || err) }));
            return { ok: false };
          } finally { inFlight.current = null; }
        })();
        return inFlight.current;
      }, []);
      React.useEffect(() => {
        live.current = true;
        let timer, stopped = false, failures = 0, polling = false;
        const poll = async () => {
          clearTimeout(timer);
          if (stopped || document.hidden || polling) return;
          polling = true;
          const reply = await refresh();
          polling = false;
          failures = reply.ok ? 0 : failures + 1;
          // Runtime indicators have their own shared lightweight status feed.
          // Read the full catalog less often; actions still refresh immediately.
          const delay = reply.ok && reply.live_status === 'connecting' ? 2000 : Math.min(30000, 15000 * (1 + failures));
          if (!stopped && !document.hidden) timer = setTimeout(poll, delay);
        };
        const onVisibility = () => { clearTimeout(timer); if (!document.hidden) poll(); };
        document.addEventListener('visibilitychange', onVisibility);
        poll();
        return () => { stopped = true; live.current = false; clearTimeout(timer); document.removeEventListener('visibilitychange', onVisibility); };
      }, [refresh]);
      return [state, refresh];
    }

    async function copyLocalCredential(value) {
      try { await navigator.clipboard.writeText(value); }
      catch {
        const field=document.createElement('textarea');field.value=value;field.style.cssText='position:fixed;left:-9999px;top:0';document.body.appendChild(field);
        try{field.select();if(!document.execCommand('copy'))throw Error('复制失败，请手动复制');}finally{field.value='';field.remove();}
      }
    }
    function AccountCredentials({account,envId}) {
      const [password,setPassword]=React.useState(null),[passwordBusy,setPasswordBusy]=React.useState(false);
      const [otp,setOtp]=React.useState(null),[seconds,setSeconds]=React.useState(0),[message,setMessage]=React.useState('');
      const guard=React.useRef({live:false,generation:0}),passwordTimer=React.useRef(null),otpAction=React.useRef(null);
      const requestAccount=async(action)=>{const r=await call(MANAGE+'accounts',{env_id:envId,account_id:account.account_id,action,...(action==='credential'?{field:'password'}:{})});if(!r.ok)throw Error(r.error||'读取失败，请重试');return r;};
      const hidePassword=()=>{guard.current.generation++;clearTimeout(passwordTimer.current);setPassword(null);setPasswordBusy(false);};
      React.useEffect(()=>{
        let stopped=false,loading=false,deadline=0,failed=false,otpGeneration=0;
        guard.current.live=true;
        const load=async()=>{
          if(stopped||loading||document.hidden||!account.has_otp)return;
          loading=true;const generation=otpGeneration;
          try{const r=await requestAccount('totp');if(stopped||document.hidden||generation!==otpGeneration)return;
            deadline=performance.now()+Math.max(0,r.expires_at-(r.server_time||Date.now()/1000))*1000;
            failed=false;setOtp({code:r.code,deadline});setSeconds(Math.max(0,Math.ceil((deadline-performance.now())/1000)));
          }catch(e){if(!stopped&&generation===otpGeneration){failed=true;setOtp({error:e.message});}}finally{loading=false;}
        };
        otpAction.current=load;
        const tick=()=>{if(document.hidden||stopped)return;const remaining=Math.max(0,Math.ceil((deadline-performance.now())/1000));setSeconds(remaining);if(!remaining&&!failed){setOtp(null);load();}};
        const visibility=()=>{hidePassword();if(document.hidden){otpGeneration++;setOtp(null);deadline=0;}else{failed=false;load();}};
        const blur=()=>hidePassword();
        document.addEventListener('visibilitychange',visibility);window.addEventListener('blur',blur);
        if(account.has_otp)load();
        const timer=account.has_otp?setInterval(tick,1000):null;
        return()=>{stopped=true;guard.current.live=false;guard.current.generation++;clearInterval(timer);clearTimeout(passwordTimer.current);otpAction.current=null;document.removeEventListener('visibilitychange',visibility);window.removeEventListener('blur',blur);};
      },[envId,account.account_id,account.revision,account.has_otp]);
      const reveal=async()=>{
        if(password){hidePassword();return;}if(passwordBusy)return;
        const generation=guard.current.generation;setPasswordBusy(true);setMessage('');
        try{const r=await requestAccount('credential');if(!guard.current.live||generation!==guard.current.generation||document.hidden)return;
          const deadline=performance.now()+Math.min(15000,Math.max(0,(r.expires_at||Date.now()/1000+15)-(r.server_time||Date.now()/1000))*1000);
          setPassword({value:r.value,deadline});passwordTimer.current=setTimeout(hidePassword,Math.max(1,deadline-performance.now()));
        }catch(e){if(guard.current.live&&generation===guard.current.generation)setMessage(e.message);}finally{if(guard.current.live&&generation===guard.current.generation)setPasswordBusy(false);}
      };
      const copy=async(value,label)=>{try{await copyLocalCredential(value);if(guard.current.live)setMessage(label+'已复制');}catch(e){if(guard.current.live)setMessage(e.message);}};
      return h('section',{className:'lm-section lm-credentials','aria-label':'本机账号凭据'},h('h3',null,'本机凭据'),
        h('div',{className:'lm-credential-row'},h('span',null,'密码'),h('code',{className:'lm-password','data-password-value':true},password?.value||(account.has_password?'••••••••':'未保存')),
          account.has_password&&h('div',{className:'lm-actions'},h('button',{type:'button',onClick:reveal,disabled:passwordBusy},password?'隐藏密码':passwordBusy?'读取中…':'查看密码'),h('button',{type:'button',disabled:!password,onClick:()=>{if(password?.deadline>performance.now())copy(password.value,'密码');else hidePassword();}},'复制密码'))),
        h('div',{className:'lm-credential-row'},h('span',null,'2FA'),account.has_otp?h(React.Fragment,null,h('button',{type:'button',className:'lm-otp-code',disabled:!otp?.code||seconds<=0,onClick:()=>{if(otp?.deadline>performance.now()+500)copy(otp.code,'验证码');else otpAction.current?.();}},otp?.code&&seconds>0?otp.code:'······'),h('span',{className:'lm-muted'},otp?.error?'生成失败':seconds>0?seconds+' 秒后更新':'正在更新…'),otp?.error&&h('button',{type:'button',onClick:()=>otpAction.current?.()},'重试')):h('span',{className:'lm-muted'},'未设置')),
        h('p',{className:'lm-muted'},'密码默认隐藏，显示后 15 秒自动隐藏。2FA 自动更新，点击验证码即可复制。'),message&&h('p',{className:'lm-notice',role:'status'},message));
    }

    function AccountFields({value,onChange,platforms,onRemove}) {
      const [search,setSearch]=React.useState('');
      const update=(key,v)=>onChange({...value,[key]:v});
      const p=platforms.find(p=>p.id===value.platform_id);
      const field=(key,label,type='text')=>h('label',{className:'lm-field'},h('span',null,label),h('input',{name:'account.'+key,type,value:value[key]||'',autoComplete:'off',onChange:e=>update(key,e.target.value)}));
      const credential=(key,label)=>h('label',{className:'lm-field'},h('span',null,label),h('div',{className:'lm-inline'},h('select',{'aria-label':label+'操作',value:value[key+'_action']||'keep',onChange:e=>update(key+'_action',e.target.value)},h('option',{value:'keep'},value.account_id?'保留原值':'未设置'),h('option',{value:'replace'},'设置 / 替换'),h('option',{value:'clear'},'清除')),h('input',{name:'account.'+key,type:'password',autoComplete:'new-password',value:value[key]||'',placeholder:key==='otp_secret'?'验证器密钥，不是六位验证码':'输入新密码',onChange:e=>onChange({...value,[key]:e.target.value,[key+'_action']:'replace'})})));
      return h('section',{className:'lm-account-edit','data-account-draft':value.client_id||value.account_id},
        h('div',{className:'lm-head'},h('strong',{className:'lm-icon-title'},platformIcon(value,20),value.account_id?'编辑账号':'新增账号',p&&h('span',{className:'lm-muted'},' · '+p.name)),h('button',{type:'button',onClick:onRemove},'移除此条')),
        h('div',{className:'lm-grid'},
          h('label',{className:'lm-field'},h('span',null,'查找平台'),h('input',{type:'search',value:search,placeholder:'Facebook、Amazon 等',onChange:e=>setSearch(e.target.value)})),
          h('label',{className:'lm-field'},h('span',null,'平台 *'),h('div',{className:'lm-platform-control'},platformIcon(value,20),h('select',{name:'account.platform_id',value:value.platform_id||'',onChange:e=>{const p=platforms.find(p=>p.id===e.target.value);onChange({...value,platform_id:p?.id||'',platform:p?.name||'',url:p?.url||'',site:''});}},h('option',{value:''},'请选择平台'),...platforms.filter(p=>p.id===value.platform_id||p.name.toLowerCase().includes(search.toLowerCase())).map(p=>h('option',{key:p.id,value:p.id},p.category+' · '+p.name))))),
          !!p?.sites?.length&&h('label',{className:'lm-field'},h('span',null,'国家 / 站点'),h('select',{name:'account.site',value:value.site||'',onChange:e=>{const s=p.sites.find(s=>s.name===e.target.value);onChange({...value,site:s?.name||'',url:s?.url||p.url});}},h('option',{value:''},'选择站点'),...p.sites.map(s=>h('option',{key:s.name,value:s.name},s.name)))),
          field('url','网站 / 店铺登录地址 *','url'),field('username','用户名 / 昵称'),field('login','登录账号（邮箱、手机号或 ID）*'),field('label','账号备注'),
          credential('password',value.has_password?'密码（已保存）':'密码'),credential('otp_secret',value.has_otp?'2FA 验证器密钥（已保存）':'2FA 验证器密钥（选填）')),
        value.account_id&&h('label',{className:'lm-check'},h('input',{type:'checkbox',checked:!!value.confirm_identity_change,onChange:e=>update('confirm_identity_change',e.target.checked)}),'更换平台或登录账号时，清除旧密码、2FA 和旧登录会话，使用本次新输入'),
        value.error&&h('p',{role:'alert',className:'lm-error'},value.error));
    }

    function WorkspaceDialog({title,context,onClose,children,footer,locked=false}) {
      const ref=React.useRef(null),closeRef=React.useRef(onClose);
      closeRef.current=onClose;
      React.useLayoutEffect(()=>{const el=ref.current;el.showModal();return()=>{if(el.open)el.close();};},[]);
      return h('dialog',{ref,className:'lm-modal','aria-label':title,'aria-modal':true,
        onCancel:e=>{e.preventDefault();if(!locked)closeRef.current();},
        onClick:e=>{if(e.target!==e.currentTarget||locked)return;const r=e.currentTarget.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeRef.current();}},
        h('header',{className:'lm-modal-head'},h('div',null,h('h3',null,title),context&&h('small',null,context)),h('button',{type:'button','aria-label':'关闭'+title,disabled:locked,onClick:onClose},'关闭')),
        h('div',{className:'lm-modal-body'},children),footer&&h('footer',{className:'lm-modal-foot'},footer));
    }

    let pendingEnvironmentCreate=false;
    function openEnvironmentCreator(){
      const navigation=document.querySelector('nav button.wtg9ga_panelRow[aria-label="我的环境"]');
      if(!navigation)return false;
      pendingEnvironmentCreate=true;
      if(!document.querySelector('.lan-manager'))navigation.click();
      window.dispatchEvent(new Event('lan-env-create'));
      return true;
    }

    function BatchImportPage({hidden,envs,platforms,onChanged,workspaceNotice,connectionWarning}){
      const [kind,setKind]=React.useState('environments'),[method,setMethod]=React.useState('generate');
      const [text,setText]=React.useState(''),[generate,setGenerate]=React.useState({prefix:'环境-',start:1,count:5,group:'',locale:'',start_url:'',proxies:''});
      const [job,setJob]=React.useState(null),[error,setError]=React.useState(''),[busy,setBusy]=React.useState(false);
      const [historyOpen,setHistoryOpen]=React.useState(false),[history,setHistory]=React.useState([]),[filter,setFilter]=React.useState('all'),[search,setSearch]=React.useState('');
      const gate=React.useRef(false),requestId=React.useRef(''),latest=React.useRef(onChanged),inputs=React.useRef({}),jobEpoch=React.useRef(0);
      latest.current=onChanged;
      const running=job&&['running','cancelling'].includes(job.status);
      const response=async p=>{const r=await call(MANAGE+'import',p);if(!r.ok)throw Error(r.error||'导入服务暂不可用');return r;};
      const api=async p=>(await response(p)).job;
      const remember=j=>{jobEpoch.current++;setJob(j);setFilter('all');setSearch('');try{if(j)sessionStorage.setItem('lan-env-import-job',j.id);else sessionStorage.removeItem('lan-env-import-job');}catch{}};
      const run=async fn=>{if(gate.current)return;gate.current=true;setBusy(true);setError('');try{await fn();}catch(e){setError(e.message||'操作失败，请重试');}finally{gate.current=false;setBusy(false);}};
      React.useEffect(()=>{let alive=true;const epoch=jobEpoch.current;let id;try{id=sessionStorage.getItem('lan-env-import-job');}catch{}if(id)api({job_id:id}).then(j=>{if(alive&&j&&epoch===jobEpoch.current){setKind(j.kind);setJob(j);}}).catch(()=>{if(alive&&epoch===jobEpoch.current)setError('上次任务读取失败，可在“任务记录”中找回。');});return()=>{alive=false;};},[]);
      React.useEffect(()=>{if(!running)return;let alive=true,t;const poll=async()=>{try{const j=await api({job_id:job.id});if(!alive)return;setJob(j);setError('');if(['running','cancelling'].includes(j.status))t=setTimeout(poll,600);else latest.current();}catch{if(alive){setError('状态连接暂时断开，正在重连；任务不会重复提交。');t=setTimeout(poll,1800);}}};t=setTimeout(poll,300);return()=>{alive=false;clearTimeout(t);};},[job?.id,running]);
      const button=(label,fn,primary=false,disabled=false)=>h('button',{type:'button',onClick:()=>run(fn),disabled:busy||disabled,className:primary?'lm-primary':''},label);
      const selectKind=next=>run(async()=>{if(job||kind===next)return;jobEpoch.current++;inputs.current[kind]=text;setKind(next);setText(inputs.current[next]||'');requestId.current='';setError('');});
      const showHistory=()=>run(async()=>{const r=await response({action:'history'});setHistory(r.jobs||[]);setHistoryOpen(true);});
      const clearHistory=id=>run(async()=>{await api({action:'discard',job_id:id});setHistory(old=>old.filter(j=>j.id!==id));if(job?.id===id)remember(null);});
      const download=(content,name)=>{const u=URL.createObjectURL(new Blob(['\ufeff'+content],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);};
      const csv=rows=>rows.map(row=>row.map(v=>'"'+String(v??'').replaceAll('"','""')+'"').join(',')).join('\r\n');
      const headers={environments:['环境名称','分组','代理','首次打开页面','语言'],proxies:['环境','代理'],accounts:['环境','平台','登录账号','密码','2FA密钥','用户名','网站','备注']};
      const first=envs[0]?.env_id||'环境编号';
      const template=()=>download(csv([headers[kind],kind==='environments'?['运营-001','','direct','https://www.google.com/','zh-CN']:kind==='proxies'?[first,'socks5://127.0.0.1:1080']:[first,'facebook','operator@example.com','','','','','']]),'蓝豆-'+({environments:'批量新建',proxies:'代理导入',accounts:'账号导入'}[kind])+'模板.csv');
      const field=(key,label,type='text')=>h('label',{className:'lm-field'},h('span',null,label),h('input',{type,value:generate[key],min:key==='count'?1:key==='start'?0:undefined,max:key==='count'?200:undefined,onChange:e=>{requestId.current='';setGenerate({...generate,[key]:type==='number'?Number(e.target.value):e.target.value});}}));
      const preview=()=>run(async()=>{if(!requestId.current)requestId.current=crypto.randomUUID().replaceAll('-','');const id=requestId.current;try{sessionStorage.setItem('lan-env-import-job',id);}catch{}const j=await api({action:'preview',kind,request_id:id,...(kind==='environments'&&method==='generate'?{generate}:{text})});remember(j);});
      const changePlan=()=>run(async()=>{await api({action:'discard',job_id:job.id});remember(null);requestId.current='';});
      const editPreview=async()=>{const source=(await response({action:'source',job_id:job.id})).source;await api({action:'discard',job_id:job.id});remember(null);requestId.current='';setText(source?.text||'');inputs.current[kind]=source?.text||'';if(source?.generate)setGenerate(source.generate);setMethod(source?.method||'csv');if(!source)setError('此旧任务没有保留原始输入，请重新粘贴或上传文件。');};
      const action=name=>run(async()=>{const j=await api({action:name,job_id:job.id});remember(j);if(['apply','retry'].includes(name)){setText('');inputs.current[kind]='';setGenerate(g=>({...g,proxies:''}));}if(j?.status==='complete')latest.current();});
      const stateLabel={preview:'待确认',running:'正在导入',cancelling:'正在停止',cancelled:'已停止',complete:'全部完成',partial:'部分未完成',interrupted:'已中断，可安全继续',unavailable:'记录无法读取'};
      const filtered=(job?.rows||[]).filter(r=>(filter==='all'||filter==='errors'&&['failed','invalid'].includes(r.status)||filter==='pending'&&['pending','running'].includes(r.status)||filter==='success'&&r.status==='success')&&[r.label,r.env_id,r.summary,r.error,r.line].join(' ').toLowerCase().includes(search.trim().toLowerCase()));
      return h('section',{hidden,className:'lm-import-page','aria-label':'批量新建与导入'},
        h('nav',{className:'lm-subnav','aria-label':'批量操作类型'},...[['environments','批量新建'],['proxies','代理导入'],['accounts','账号导入']].map(([k,title])=>h('button',{key:k,type:'button','aria-current':!historyOpen&&kind===k?'page':undefined,disabled:busy||!!job||historyOpen,onClick:()=>selectKind(k)},title)),h('button',{type:'button',className:'lm-import-history-toggle',disabled:busy,'aria-current':historyOpen?'page':undefined,onClick:showHistory},'任务记录')),
        h('div',{className:'lm-import-feedback',role:'status','aria-live':'polite'},error||workspaceNotice||connectionWarning||'每批最多 200 行，确认前可逐行检查。数据仅保存在本机。'),
        historyOpen&&h('section',{'aria-label':'本机导入任务记录'},h('div',{className:'lm-head'},h('h3',null,'本机任务记录'),button('返回导入',async()=>setHistoryOpen(false))),h('p',{className:'lm-muted'},'可找回其它窗口创建的任务。清除记录只删除本机导入记录，已创建的环境和账号会保留。'),!history.length&&h('p',{className:'lm-empty'},'暂无导入任务'),...history.map(j=>h('article',{key:j.id,className:'lm-import-history-row','data-task-id':j.id},h('div',null,h('strong',null,({environments:'批量新建环境',proxies:'代理导入',accounts:'账号导入'}[j.kind])||'导入记录'),h('small',null,(j.created_at?new Date(j.created_at*1000).toLocaleString():'')+' · '+(stateLabel[j.status]||j.status)+' · '+j.done+'/'+j.total+' 已完成')),h('div',{className:'lm-actions'},j.status!=='unavailable'&&button('查看任务',async()=>{const item=await api({job_id:j.id});inputs.current[kind]=text;setKind(item.kind);setText(inputs.current[item.kind]||'');remember(item);setHistoryOpen(false);}),h('button',{type:'button',disabled:busy||['running','cancelling'].includes(j.status),onClick:()=>clearHistory(j.id)},'清除记录'))))),
        !historyOpen&&!job&&h('fieldset',{disabled:busy,className:'lm-import-form'},
          kind==='environments'&&h('div',{className:'lm-actions lm-section'},...['generate','csv'].map(m=>h('label',{key:m,className:'lm-check'},h('input',{type:'radio',name:'batch-method',checked:method===m,onChange:()=>{setMethod(m);requestId.current='';}}),m==='generate'?'按名称批量生成':'从表格导入'))),
          kind==='environments'&&method==='generate'?h('section',{className:'lm-section'},h('div',{className:'lm-grid'},field('prefix','名称前缀 *'),field('start','起始编号','number'),field('count','新建数量（1–200）','number'),field('group','分组'),field('locale','语言，例如 zh-CN'),field('start_url','首次打开页面')),h('label',{className:'lm-field'},h('span',null,'按顺序分配代理（选填，每行一个）'),h('textarea',{'aria-label':'按顺序分配代理（选填，每行一个）',rows:4,value:generate.proxies,placeholder:'socks5://127.0.0.1:1080\nhttp://127.0.0.1:8080',onChange:e=>{setGenerate({...generate,proxies:e.target.value});requestId.current='';}})),h('p',{className:'lm-muted'},'留空使用直接连接；填写后代理行数须与新建数量相等。每个环境独立生成指纹种子与网站数据分区。')):
          h('section',{className:'lm-section'},h('div',{className:'lm-actions'},button('下载 CSV 模板',async()=>template()),h('label',{className:'lm-file'},h('span',null,'上传 CSV / TSV'),h('input',{type:'file',accept:'.csv,.tsv,.txt',disabled:busy,onChange:e=>{const f=e.target.files?.[0];e.target.value='';if(f)run(async()=>{if(f.size>1024*1024)throw Error('文件不能超过 1MB');setText(await f.text());requestId.current='';});}}))),h('label',{className:'lm-field'},h('span',null,'粘贴表格内容（含表头）'),h('textarea',{'aria-label':'粘贴表格内容（含表头）',rows:8,value:text,spellCheck:false,autoComplete:'off',onChange:e=>{setText(e.target.value);requestId.current='';},placeholder:headers[kind].join(',')})),h('p',{className:'lm-muted'},'支持 CSV 或从 Excel 直接复制；含逗号的值用双引号包住。必填：'+({environments:'环境名称',proxies:'环境、代理',accounts:'环境、平台、登录账号'}[kind])+'。'),kind==='accounts'&&h('p',{className:'lm-muted'},'同一环境可填写多行账号。密码等选填；2FA 填验证器密钥，不能填六位验证码。只新增账号，不自动登录网站。')),
          kind!=='accounts'&&h('p',{className:'lm-muted'},'代理格式：http://主机:端口、https://主机:端口、socks5://主机:端口、socks4://主机:端口，或 direct / system。当前内核不支持用户名密码认证代理。'),
          kind!=='environments'&&h('details',{className:'lm-section'},h('summary',null,'查看环境编号'+(kind==='accounts'?'与平台名称':'')),h('p',{className:'lm-muted'},'环境使用准确编号或唯一名称；同名环境请用编号。'),h('div',{className:'lm-import-reference'},...envs.map(e=>h('div',{key:e.env_id},e.env_id+' · '+e.display_name))),kind==='accounts'&&h('p',null,platforms.map(p=>p.name+' ('+p.id+')').join('、'))),
          h('footer',{className:'lm-footer lm-actions'},button('恢复任务',async()=>{let id;try{id=sessionStorage.getItem('lan-env-import-job');}catch{}if(!id)throw Error('没有待恢复的任务');const j=await api({job_id:id});remember(j);if(j)setKind(j.kind);}),h('button',{type:'button',className:'lm-primary',disabled:busy,onClick:preview},busy?'正在检查…':'预览并检查'))),
        !historyOpen&&job&&h(React.Fragment,null,
          h('section',{className:'lm-section'},h('div',{className:'lm-head'},h('h3',null,stateLabel[job.status]||job.status),h('span',null,job.done+' / '+job.total+' 已完成'+(job.failed?' · '+job.failed+' 项失败':'')+(job.invalid?' · '+job.invalid+' 行需修正':''))),h('progress',{max:job.total,value:job.processed??job.done,'aria-label':'导入进度'}),h('p',{className:'lm-muted'},job.status==='preview'?'以下内容尚未写入。请核对后确认导入。':job.status==='complete'?'导入完成。可返回环境列表查看，或开始下一批。':running?'可返回环境列表，任务会继续。停止会保留已完成条目。':'已完成条目会保留。连接或保存故障可重试；配置冲突需检查数据后重新导入。'),
            h('div',{className:'lm-actions'},job.status==='preview'&&button('返回修改',editPreview),job.status==='preview'&&button('确认导入',async()=>{const j=await api({action:'apply',job_id:job.id});remember(j);setText('');inputs.current[kind]='';setGenerate(g=>({...g,proxies:''}));if(j?.status==='complete')latest.current();},true,job.invalid>0),running&&button(job.status==='cancelling'?'正在停止…':'停止后续导入',async()=>remember(await api({action:'cancel',job_id:job.id})),false,job.status==='cancelling'),!running&&job.status!=='preview'&&(job.resumable??job.total-job.done)>0&&h('button',{type:'button',disabled:busy,onClick:()=>action('retry'),className:'lm-primary'},'重试未完成项'),!running&&job.status!=='preview'&&h('button',{type:'button',disabled:busy,onClick:changePlan},'清除任务记录，开始下一批'),job.rows.some(r=>r.error)&&button('下载错误报告',async()=>download(csv([['行号','状态','原因','建议'],...job.rows.filter(r=>r.error).map(r=>[r.line,r.status,r.error,r.retryable===false?'检查后重新导入':'修正后重试'])]),'蓝豆导入错误报告.csv')))),
          h('div',{className:'lm-import-result-tools'},h('input',{type:'search','aria-label':'搜索导入结果',placeholder:'搜索环境、账号或行号',value:search,onChange:e=>setSearch(e.target.value)}),h('select',{'aria-label':'筛选导入结果',value:filter,onChange:e=>setFilter(e.target.value)},...Object.entries({all:'全部',errors:'需处理',pending:'待执行',success:'已完成'}).map(([v,t])=>h('option',{key:v,value:v},t))),h('small',{className:'lm-muted','aria-live':'polite'},filtered.length+' / '+job.total+' 条')),
          h('div',{className:'lm-import-rows',role:'list'},!filtered.length&&h('p',{className:'lm-empty'},'没有符合条件的条目'),...filtered.map(r=>h('div',{key:r.line,className:'lm-import-row',role:'listitem','data-status':r.status},h('span',{className:'lm-muted'},r.line),h('div',null,h('strong',null,r.label),h('small',null,r.summary||r.env_id||''),r.error&&h('p',{className:'lm-error'},r.error,r.retryable===false&&h('small',null,'需重新检查数据，直接重试不会覆盖当前配置。'))),h('span',null,({pending:'待执行',running:'保存中',success:'已完成',failed:'失败',invalid:'需修正'}[r.status])||r.status))))));
    }

    function LanEnvPanel() {
      const [state,refresh]=useEnvs();
      const [runtime,setRuntime]=React.useState({states:{}});
      React.useEffect(()=>runtimeStatus.subscribe(setRuntime),[]);
      const actualActive=runtime.live_status==='ready'?runtime.active:state.active;
      const [tab,setTab]=React.useState('envs'),[notice,setNotice]=React.useState(''),[busy,setBusy]=React.useState('');
      const gate=React.useRef(false),panel=React.useRef(null),content=React.useRef(null),editorReturn=React.useRef(null),scrollPositions=React.useRef({}),draftTimer=React.useRef(null),draftSequence=React.useRef(Promise.resolve());
      const [envDetail,setEnvDetail]=React.useState(''),[editorSection,setEditorSection]=React.useState('basic');
      const [accountEditing,setAccountEditing]=React.useState(false),[accountDetail,setAccountDetail]=React.useState('');
      const [dataSection,setDataSection]=React.useState(''),[proxyDetail,setProxyDetail]=React.useState(false),[bulkDialog,setBulkDialog]=React.useState(false);
      const [fingerprintEnv,setFingerprintEnv]=React.useState(''),[fingerprintData,setFingerprintData]=React.useState(null);
      const [view,setView]=React.useState(()=>{try{return JSON.parse(localStorage.getItem('lm-workspace-view'))||{};}catch{return {};}});
      const query=view.query||'',group=view.group||'',proxyFilter=view.proxy||'',accountFilter=view.account||'',sort=view.sort||'recent';
      const [more,setMore]=React.useState(false),[selected,setSelected]=React.useState([]),[page,setPage]=React.useState(0),[size,setSize]=React.useState(25);
      const [focusEnv,setFocusEnv]=React.useState(''),[form,setForm]=React.useState(null),[initial,setInitial]=React.useState(null),[editing,setEditing]=React.useState(false);
      const [accounts,setAccounts]=React.useState([]),[drafts,setDrafts]=React.useState([]),[proxyResult,setProxyResult]=React.useState(null),[errors,setErrors]=React.useState({});
      const [maintenance,setMaintenance]=React.useState(null),[restore,setRestore]=React.useState(null),[restoreSource,setRestoreSource]=React.useState(null),[restoreIds,setRestoreIds]=React.useState([]);
      const [results,setResults]=React.useState([]),[job,setJob]=React.useState(null),[jobFilter,setJobFilter]=React.useState(false);
      const [savedDraft,setSavedDraft]=React.useState(null),[draftLoaded,setDraftLoaded]=React.useState(false),[draftPrompt,setDraftPrompt]=React.useState(false),[versions,setVersions]=React.useState(null);
      const pendingNewEnvironment=React.useRef(false);
      const [transferPassword,setTransferPassword]=React.useState(''),[transfer,setTransfer]=React.useState(null),[transferPreview,setTransferPreview]=React.useState(null);
      const [bulkGroup,setBulkGroup]=React.useState(''),[bulkTags,setBulkTags]=React.useState(''),[manualCodes,setManualCodes]=React.useState({}),[conflict,setConflict]=React.useState(null);
      const platforms=state.platforms||[],current=state.envs.find(e=>e.env_id===focusEnv);
      const screenKey=form?'editor:'+form.env_id:fingerprintEnv?'fingerprint:'+fingerprintEnv:tab==='envs'&&envDetail?'detail:'+envDetail:tab+':'+dataSection+':'+proxyDetail;
      React.useLayoutEffect(()=>{const el=content.current;if(el)el.scrollTop=scrollPositions.current[screenKey]||0;},[screenKey]);
      const rememberScroll=()=>{if(content.current)scrollPositions.current[screenKey]=content.current.scrollTop;};
      const btn=(text,fn,primary=false,disabled=false)=>h('button',{type:'button',onClick:fn,disabled:!!busy||disabled,className:primary?'lm-primary':''},text);
      const request=async(action,p={})=>{const r=await call(MANAGE+action,p);if(!r.ok&&!r.results)throw Object.assign(new Error(r.error||'操作失败，请重试'),{reply:r});return r;};
      const run=async(key,fn)=>{if(gate.current)return;gate.current=true;setBusy(key);setNotice('');try{await fn();}catch(e){setNotice(e.message||'操作失败');if(e.reply?.field_errors){setErrors(e.reply.field_errors);const keys=Object.keys(e.reply.field_errors);setEditorSection(keys.some(k=>k==='proxy'||k==='expected_exit_ip')?'network':keys.some(k=>['start_url','homepage','locale','accept_languages','fingerprint_seed'].includes(k))?'browser':'basic');if(content.current)content.current.scrollTop=0;}if(e.reply?.conflict){const fresh=await refresh(true);setConflict(fresh.envs?.find(x=>x.env_id===form?.env_id));}}finally{gate.current=false;setBusy('');}};
      const updateView=p=>{setView(old=>({...old,...p}));setPage(0);};
      const loadAccounts=async env=>{if(!env){setAccounts([]);return;}const r=await request('accounts',{env_id:env});setAccounts(r.accounts);};
      const openFingerprint=env=>run('fingerprint',async()=>{rememberScroll();setFingerprintEnv(env.env_id);setFingerprintData(null);setNotice('');setFingerprintData(await request('fingerprint',{env_id:env.env_id}));});
      const viewFingerprintFromEditor=env=>run('fingerprint-editor',async()=>{const draft=draftValue();await flushDraft();setSavedDraft(draft);setForm(null);setDrafts([]);setTab('envs');setEnvDetail('');setFingerprintEnv(env.env_id);setFingerprintData(await request('fingerprint',{env_id:env.env_id}));setNotice('编辑草稿已保存在本机，可从提示继续编辑');});
      const loadMaintenance=async()=>setMaintenance(await request('maintenance'));
      const enqueueDraft=(action,data={})=>{draftSequence.current=draftSequence.current.catch(()=>{}).then(()=>request('draft',{action,...data}));return draftSequence.current;};
      const draftValue=()=>({form,initial,editing,drafts,focusEnv,tab,editorSection,editorReturn:editorReturn.current,at:Date.now()});
      const flushDraft=()=>{clearTimeout(draftTimer.current);return form||drafts.length?enqueueDraft('save',{draft:draftValue()}):Promise.resolve();};
      React.useEffect(()=>{if(!focusEnv&&state.envs.length)setFocusEnv(state.active||state.envs[0].env_id);},[state.envs.length,state.active]);
      React.useEffect(()=>{if(focusEnv)loadAccounts(focusEnv).catch(e=>setNotice(e.message));},[focusEnv]);
      React.useEffect(()=>{request('draft').then(r=>setSavedDraft(r.draft)).catch(()=>{}).finally(()=>setDraftLoaded(true));request('proxy-job').then(r=>setJob(r.job)).catch(()=>{});},[]);
      React.useEffect(()=>{if(!draftLoaded||!pendingNewEnvironment.current)return;pendingNewEnvironment.current=false;if(savedDraft)setDraftPrompt(true);else begin();},[draftLoaded,savedDraft]);
      React.useEffect(()=>{try{localStorage.setItem('lm-workspace-view',JSON.stringify(view));}catch{}},[view]);
      React.useEffect(()=>{if(!form&&!drafts.length)return;clearTimeout(draftTimer.current);draftTimer.current=setTimeout(()=>enqueueDraft('save',{draft:draftValue()}).catch(()=>setNotice('草稿未保存，请保持当前页面并重试')),500);return()=>clearTimeout(draftTimer.current);},[form,drafts]);
      React.useEffect(()=>{if(!form&&!drafts.length)return;const f=e=>{e.preventDefault();e.returnValue='';};window.addEventListener('beforeunload',f);return()=>window.removeEventListener('beforeunload',f);},[!!form,drafts.length]);
      React.useEffect(()=>{if(!job||!['running','cancelling'].includes(job.status))return;let stop=false,t;const poll=async()=>{try{const r=await request('proxy-job',{job_id:job.id});if(!stop){if(!r.job)setNotice('上次检测记录已过期，可重新检测');setJob(r.job);if(r.job&&['running','cancelling'].includes(r.job.status))t=setTimeout(poll,700);else refresh();}}catch{if(!stop){setNotice('检测状态暂时断开，正在重连，不会重复提交检测');t=setTimeout(poll,2000);}}};t=setTimeout(poll,500);return()=>{stop=true;clearTimeout(t);};},[job?.id,job?.status]);
      const goto=(next,env)=>run('navigate',async()=>{rememberScroll();await flushDraft();if(form||drafts.length)setSavedDraft(draftValue());setForm(null);setDrafts([]);setTab(next);setDataSection('');setProxyDetail(false);setFingerprintEnv('');setFingerprintData(null);setAccountDetail('');setAccountEditing(false);setEnvDetail('');setResults([]);if(env)setFocusEnv(env);if(next==='data')await loadMaintenance();});
      const freshAccount=()=>({client_id:crypto.randomUUID(),platform_id:'',login:'',username:'',url:'',password_action:'keep',otp_secret_action:'keep'});
      const beginEditor=(env,copy=false)=>{editorReturn.current={tab,envDetail,dataSection,proxyDetail};const f=env?{...configOf(env),revision:env.revision}:{...configOf({}),env_id:'env-'+crypto.randomUUID().slice(0,8),proxy:'direct'};if(copy){let i=1,name;do{name=env.display_name+' 副本'+(i===1?'':' '+i);i++;}while(state.envs.some(e=>e.display_name===name));f.env_id='env-'+crypto.randomUUID().slice(0,8);f.display_name=name;f.fingerprint_seed='';delete f.revision;}setEditorSection('basic');setForm(f);setInitial(f);setEditing(!!env&&!copy);setDrafts([]);setErrors({});setProxyResult(null);setConflict(null);setNotice(copy?'已复制配置，代理仍沿用来源。账号、网站登录数据和原指纹不会复制。':'');if(content.current)content.current.scrollTop=0;};
      const begin=(env,copy=false)=>run('editor',async()=>{rememberScroll();await flushDraft();beginEditor(env,copy);});
      const beginNewEnvironment=()=>{if(!draftLoaded){pendingNewEnvironment.current=true;setNotice('正在检查未完成草稿…');return;}if(savedDraft){setDraftPrompt(true);return;}begin();};
      React.useEffect(()=>{const consume=()=>{if(!pendingEnvironmentCreate||gate.current)return;pendingEnvironmentCreate=false;if(!draftLoaded){pendingNewEnvironment.current=true;return;}if(savedDraft)setDraftPrompt(true);else begin();};window.addEventListener('lan-env-create',consume);consume();return()=>window.removeEventListener('lan-env-create',consume);},[busy,draftLoaded,savedDraft]);
      const removeAccountDraft=i=>run('remove-draft',async()=>{const next=drafts.filter((_,j)=>j!==i);setDrafts(next);clearTimeout(draftTimer.current);if(!form&&!next.length){setAccountEditing(false);setSavedDraft(null);await enqueueDraft('clear');}else await enqueueDraft('save',{draft:{...draftValue(),drafts:next}});});
      const accountEditor=h(React.Fragment,null,...drafts.map((a,i)=>h(AccountFields,{key:a.client_id||a.account_id,value:a,platforms,onChange:v=>setDrafts(old=>old.map((x,j)=>j===i?v:x)),onRemove:()=>removeAccountDraft(i)})),btn('＋ 继续添加账号',()=>setDrafts(old=>[...old,freshAccount()])));
      const saveAccounts=async env=>{if(!drafts.length)return true;const r=await request('accounts',{action:'save',env_id:env,accounts:drafts});setResults(r.results.map(r=>({...r,label:r.account?.login||drafts.find(d=>d.client_id===r.client_id)?.login||r.account_id})));const failed=r.results.filter(r=>!r.ok);setDrafts(drafts.filter(d=>failed.some(r=>r.client_id===d.client_id||r.account_id&&r.account_id===d.account_id)).map(d=>({...d,error:failed.find(r=>r.client_id===d.client_id||r.account_id===d.account_id)?.error})));await loadAccounts(env);return r.ok;};
      const save=async open=>{setErrors({});const r=await request('save',{...form,create_only:!editing,allow_duplicate_name:!!form.allow_duplicate_name});setEditing(true);setInitial({...form,revision:r.revision});setForm({...form,revision:r.revision});let testWarning='';if(proxyResult?.proxy===form.proxy){try{await request('proxy-attach',{env_id:form.env_id,receipt:proxyResult.receipt});}catch{testWarning='；检测记录未关联，请在代理页重新检测';}}const accountOk=await saveAccounts(form.env_id);if(!accountOk){setEditorSection('accounts');setNotice('环境已保存；部分账号未保存，请修正后重试失败项。');await refresh();return;}const env=form.env_id;setView({sort:'updated'});setPage(0);setFocusEnv(env);setEnvDetail(env);setTab('envs');setProxyDetail(false);setDataSection('');setForm(null);setDrafts([]);setSavedDraft(null);clearTimeout(draftTimer.current);await enqueueDraft('clear');await refresh();setNotice((r.restart_required?.length?'已保存，网络或浏览器身份变更需重启蓝豆浏览器后完整生效。':'环境和账号已保存')+testWarning);if(r.warning)setNotice(r.warning);if(open)await request('open',{env_id:env});};
      const input=(key,label,wide=false)=>h('label',{className:'lm-field'+(wide?' lm-wide':'')},h('span',null,label),h(key==='notes'?'textarea':'input',{name:key,value:form?.[key]||'',disabled:!!busy,onChange:e=>{setForm({...form,[key]:e.target.value});setErrors({...errors,[key]:''});if(['proxy','expected_exit_ip'].includes(key))setProxyResult(null);},'aria-invalid':!!errors[key]}),errors[key]&&h('small',{role:'alert',className:'lm-error'},errors[key]));
      const select=(label,value,fn,opts)=>h('label',{className:'lm-field'},h('span',null,label),h('select',{'aria-label':label,value,onChange:e=>fn(e.target.value)},...opts.map(([v,t])=>h('option',{key:v,value:v},t))));
      const envPicker=select('选择环境',focusEnv,v=>{setAccountDetail('');if(drafts.length){setNotice('请先保存或取消当前账号编辑，再切换环境');return;}setAccounts([]);setFocusEnv(v);setResults([]);},state.envs.map(e=>[e.env_id,e.display_name+' · '+e.env_id]));
      const download=(value,name)=>{const u=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);};
      const file=(label,fn)=>h('label',{className:'lm-file'},h('span',null,label),h('input',{type:'file',accept:'.json,application/json',onChange:e=>{const f=e.target.files?.[0];if(f)run('import',async()=>{if(f.size>4*1024*1024)throw Error('文件不能超过 4MB');await fn(JSON.parse(await f.text()));});e.target.value='';}}));
      const startJob=ids=>run('inspection',async()=>{const rid=crypto.randomUUID();let r;try{r=await request('proxy-job',{action:'start',env_ids:ids,request_id:rid});}catch(e){const recovered=await request('proxy-job');if(recovered.job?.request_id===rid||e.reply?.job)r={job:e.reply?.job||recovered.job};else throw e;}setJob(r.job);setProxyDetail(true);setTab('proxy');});
      const accountAction=(a,action)=>run('account-'+action,async()=>{const r=await request('accounts',{env_id:focusEnv,account_id:a.account_id,action,code:manualCodes[a.account_id]||''});setManualCodes(old=>({...old,[a.account_id]:''}));setNotice(r.message||'操作完成');await loadAccounts(focusEnv);await refresh();});
      const status=e=>e.proxy_test?.stale?'历史检测已过期':e.proxy_test?.status==='success'?'最近检测通过':e.proxy_test?.status==='mismatch'?'出口不符':e.proxy_test?.status==='failed'?'检测失败':['','direct'].includes(e.proxy)?'直接连接':e.proxy==='system'?'系统代理':'尚未检测';
      const accountStatus={saved:'资料已保存',unknown:'登录状态待检查',login_required:'等待登录',verification_required:'需要验证',user_confirmed:'你已确认网站登录'};
      const resultView=results.length>0&&h('section',{className:'lm-notice','aria-label':'操作结果'},...results.map((r,i)=>h('div',{key:i,className:r.ok?'':'lm-error'},(r.label||r.env_id||r.account_id||'条目')+'：'+(r.ok?'已完成':r.error||'未执行'))));
      const shown=state.envs.filter(e=>[e.display_name,e.env_id,e.group,e.tags,e.notes,...(e.accounts||[]).flatMap(a=>[a.platform,a.login,a.username,a.label])].join(' ').toLowerCase().includes(query.toLowerCase())&&(!group||e.group===group)&&(!proxyFilter||(proxyFilter==='failed'?['failed','mismatch'].includes(e.proxy_test?.status):!e.proxy_test||e.proxy_test.stale))&&(!accountFilter||(accountFilter==='yes'?e.account_count>0:!e.account_count)));
      shown.sort((a,b)=>sort==='name'?a.display_name.localeCompare(b.display_name,'zh-CN',{numeric:true}):sort==='updated'?(b.updated_at||0)-(a.updated_at||0):(b.last_used_at||0)-(a.last_used_at||0));
      const actualPage=Math.min(page,Math.max(0,Math.ceil(shown.length/size)-1)),visible=shown.slice(actualPage*size,(actualPage+1)*size),groups=[...new Set(state.envs.map(e=>e.group).filter(Boolean))];
      const proxyParts=(()=>{try{const u=new URL(form?.proxy);return{type:u.protocol.slice(0,-1),host:u.hostname,port:u.port};}catch{return{type:form?.proxy||'direct',host:'',port:''};}})();
      const proxyChange=(key,value)=>{const p={...proxyParts,[key]:value};setForm({...form,proxy:['direct','system'].includes(p.type)?p.type:p.type+'://'+(p.host||'127.0.0.1')+':'+(p.port||'8080')});setProxyResult(null);};
      const subnav=(label,items,value,onChange)=>h('nav',{className:'lm-subnav','aria-label':label},...items.map(([key,title])=>h('button',{key,type:'button','aria-current':value===key?'page':undefined,onClick:()=>onChange(key)},title)));
      const editor=()=>h('form',{onSubmit:e=>{e.preventDefault();run('save',()=>save(false));},className:'lm-enter','data-editor-section':editorSection},
        subnav('环境设置分类',[['basic','基本信息'],['network','网络代理'],['browser','浏览器设置'],['accounts','平台账号']],editorSection,setEditorSection),
        conflict&&h('section',{className:'lm-notice'},h('h3',null,'此环境已在其他页面更新'),h('p',null,'当前输入保留。请比较双方修改后选择：'),h('dl',null,...CONFIG_FIELDS.filter(k=>form[k]!==conflict[k]).flatMap(k=>[h('dt',{key:k},LABELS[k]),h('dd',{key:k+'v'},'本次：'+(form[k]||'空')+'；最新：'+(conflict[k]||'空'))])),btn('保留我的修改，合并最新配置',()=>{const merged={...configOf(conflict),revision:conflict.revision};for(const k of CONFIG_FIELDS)if(form[k]!==initial[k])merged[k]=form[k];setInitial(conflict);setForm(merged);setConflict(null);}),btn('使用最新配置',()=>{setForm({...configOf(conflict),revision:conflict.revision});setInitial(conflict);setConflict(null);})),
        editorSection==='basic'&&h('section',{className:'lm-section'},h('div',{className:'lm-grid'},input('display_name','环境名称 *'),input('group','分组'),input('tags','标签（逗号分隔）'),input('notes','备注')),h('p',{className:'lm-muted','data-env-id':form.env_id},'环境编号：'+form.env_id),h('label',{className:'lm-check'},h('input',{type:'checkbox',checked:!!form.allow_duplicate_name,onChange:e=>setForm({...form,allow_duplicate_name:e.target.checked})}),'允许与其他环境同名')),
        editorSection==='network'&&h('section',{className:'lm-section'},h('h3',null,'网络代理'),h('div',{className:'lm-grid'},select('连接方式',proxyParts.type,v=>proxyChange('type',v),[['direct','直接连接'],['system','跟随系统'],['http','HTTP'],['https','HTTPS'],['socks5','SOCKS5'],['socks4','SOCKS4']]),!['direct','system'].includes(proxyParts.type)&&h(React.Fragment,null,h('label',{className:'lm-field'},h('span',null,'服务器地址'),h('input',{name:'proxy.host',value:proxyParts.host,onChange:e=>proxyChange('host',e.target.value)})),h('label',{className:'lm-field'},h('span',null,'端口'),h('input',{name:'proxy.port',value:proxyParts.port,onChange:e=>proxyChange('port',e.target.value)})),input('expected_exit_ip','预期出口 IP（选填）'),select('IP 模式',form.ip_mode||'static',v=>setForm({...form,ip_mode:v}),[['static','静态 IP：启动时核对出口'],['dynamic','动态 IP：不提示出口变化']])) ),
          h('details',null,h('summary',null,'直接粘贴代理地址'),input('proxy','完整代理地址',true)),btn('测试当前代理',()=>run('proxy',async()=>setProxyResult(await call(MANAGE+'proxy-test',{proxy:form.proxy,expected_exit_ip:form.expected_exit_ip,ip_mode:form.ip_mode})))),proxyResult&&h('p',{role:'status'},(proxyResult.ok?'通过 · ':'失败 · ')+(proxyResult.exit_ip||proxyResult.error)+(proxyResult.exit_ip?' · 延迟 '+(proxyResult.latency_ms??'—')+' ms · '+[proxyResult.city,proxyResult.region,proxyResult.country].filter(Boolean).join('、')+' · '+(proxyResult.timezone||'时区未知')+' · '+(proxyResult.org||proxyResult.asn||'运营商未知')+(proxyResult.geo_error?' · '+proxyResult.geo_error:''):'')+'；保存环境时关联本次结果'),h('p',{className:'lm-muted'},'延迟为代理 HTTPS 出口查询往返时间；城市、时区和网络归属按出口 IP 查询。')),
        editorSection==='browser'&&h('section',{className:'lm-section'},h('h3',null,'浏览器设置'),h('div',{className:'lm-grid'},input('start_url','首次打开页面'),input('homepage','新标签页地址'),input('locale','区域语言，例如 zh-CN'),input('accept_languages','网页请求语言，例如 zh-CN,zh'),input('fingerprint_seed','指纹种子（默认自动生成）')),h('p',{className:'lm-muted'},'修改代理、区域、语言或指纹种子后，需要重启蓝豆浏览器。网站登录数据会保留。'),editing?btn('查看当前指纹信息',()=>viewFingerprintFromEditor(form)):h('p',{className:'lm-muted'},'保存环境后，可在环境详情中查看完整指纹信息。')),
        editorSection==='accounts'&&h('section',{className:'lm-section'},h('h3',null,'平台账号'),h('p',{className:'lm-muted'},editing?'已保存账号可在环境详情的账号管理中维护。本页可随环境设置一起添加新账号。':'账号可以稍后添加，也可以连续录入多条后随环境一起保存。'),accountEditor),
        h('footer',{className:'lm-savebar lm-footer'},h('small',null,'草稿自动加密保存在本机'),h('div',{className:'lm-actions'},btn('稍后继续',()=>goto('envs')),h('button',{type:'submit',className:'lm-primary',disabled:!!busy||!!conflict||editing&&same(configOf(form),configOf(initial))&&!drafts.length},'保存环境'),btn('保存并打开',()=>run('save',()=>save(true)),false,!!conflict))));
      const manageEnvironment=env=>{rememberScroll();setEnvDetail(env.env_id);setTab('envs');setResults([]);};
      const selectedEnvironment=state.envs.find(e=>e.env_id===envDetail);
      const fingerprintEnvironment=state.envs.find(e=>e.env_id===fingerprintEnv);
      const fingerprintText=value=>{
        if(value===null||value===undefined||value==='')return '—';
        if(Array.isArray(value))return value.length?value.join(', '):'—';
        if(typeof value==='object')return Object.entries(value).filter(([,v])=>v!==null&&v!==undefined&&v!=='').map(([k,v])=>k.replace(/_/g,' ')+' '+(Array.isArray(v)?v.join(', '):v)).join(' · ')||'—';
        return String(value);
      };
      const fingerprintBlock=(title,rows)=>h('section',{className:'lm-section lm-fingerprint-block'},h('h3',null,title),h('dl',null,...rows.flatMap(([label,value])=>[h('dt',{key:label},label),h('dd',{key:label+'-value'},fingerprintText(value))])));
      const fingerprintPage=()=>fingerprintEnvironment?h('section',{className:'lm-enter lm-fingerprint-page','data-fingerprint-environment':fingerprintEnvironment.env_id},
        h('div',{className:'lm-head'},h('div',null,h('h3',{className:'lm-icon-title'},environmentIcon(20),fingerprintEnvironment.display_name),h('p',{className:'lm-muted'},fingerprintEnvironment.env_id+' · '+(fingerprintData?.source==='runtime'?'实时值':'配置值'))),btn('刷新指纹',()=>openFingerprint(fingerprintEnvironment),true)),
        !fingerprintData&&h('div',{className:'lm-notice'},'正在读取此环境的指纹信息…'),
        fingerprintData&&h(React.Fragment,null,
          h('div',{className:'lm-fingerprint-summary'},h('strong',null,fingerprintData.runtime?.available?'实时运行值':'配置推算值'),h('span',null,fingerprintData.runtime?.note),h('small',null,'读取时间：'+dateLabel(fingerprintData.updated_at))),
          fingerprintBlock('浏览器与系统',[['产品',fingerprintData.browser?.product],['内核',fingerprintData.browser?.kernel],['内核版本',fingerprintData.browser?.kernel_version],['User-Agent',fingerprintData.browser?.user_agent],['平台',fingerprintData.browser?.platform],['操作系统',fingerprintData.browser?.os],['架构',fingerprintData.browser?.architecture],['自动化标记',fingerprintData.browser?.webdriver?'存在':'未检测到'],['触控点数',fingerprintData.browser?.max_touch_points]]),
          fingerprintBlock('设备与窗口',[['CPU 逻辑核心',fingerprintData.device?.hardware_concurrency],['设备内存',fingerprintData.device?.device_memory_gb&&fingerprintData.device.device_memory_gb+' GB'],['屏幕',fingerprintData.device?.screen],['窗口',fingerprintData.device?.window]]),
          fingerprintBlock('语言与时区',[['区域语言',fingerprintData.language?.locale],['请求语言',fingerprintData.language?.accept_languages],['navigator.language',fingerprintData.language?.navigator_language],['navigator.languages',fingerprintData.language?.navigator_languages],['Intl 区域',fingerprintData.language?.intl_locale],['时区',fingerprintData.language?.timezone]]),
          fingerprintBlock('图形与硬件',[['WebGL 厂商',fingerprintData.graphics?.vendor],['WebGL 渲染器',fingerprintData.graphics?.renderer],['WebGL 版本',fingerprintData.graphics?.version],['硬件档位',fingerprintData.graphics?.hardware_tier]]),
          fingerprintBlock('网络落地',[['出口 IP',fingerprintData.location?.exit_ip],['预期出口 IP',fingerprintData.location?.expected_exit_ip],['国家',fingerprintData.location?.country],['地区',fingerprintData.location?.region],['城市',fingerprintData.location?.city],['时区',fingerprintData.location?.timezone],['运营商',fingerprintData.location?.org],['延迟',fingerprintData.location?.latency_ms==null?'—':fingerprintData.location.latency_ms+' ms'],['检测状态',fingerprintData.location?.status]]),
          fingerprintBlock('字体',[['字体来源',fingerprintData.fonts?.mode==='configured'?'配置策略':'运行时网页可见字体'],['已探测字体',fingerprintData.fonts?.detected],['探测范围',fingerprintData.fonts?.tested],['探测数量',fingerprintData.fonts?.count],['说明',fingerprintData.fonts?.note||'仅探测常用字体，不读取字体文件或网站数据']]),
          h('p',{className:'lm-muted lm-fingerprint-privacy'},fingerprintData.privacy?.note+'。实时值仅在环境运行并成功连接页面时显示。')))
        :h('p',{className:'lm-empty'},'此环境已移出列表，请返回重新选择。');
      const environmentPage=()=>selectedEnvironment?h('section',{className:'lm-enter','data-environment-detail':selectedEnvironment.env_id},
        h('div',{className:'lm-head'},h('div',null,h('h3',{className:'lm-icon-title'},environmentIcon(20),selectedEnvironment.display_name),h('p',{className:'lm-muted'},selectedEnvironment.env_id+(selectedEnvironment.group?' · '+selectedEnvironment.group:''))),btn(selectedEnvironment.env_id===state.active?'回到网页':'打开环境',()=>run('open',()=>request('open',{env_id:selectedEnvironment.env_id})),true)),
        h('dl',{className:'lm-summary'},h('dt',null,'账号'),h('dd',null,selectedEnvironment.account_count+' 条'),h('dt',null,'代理状态'),h('dd',null,status(selectedEnvironment)),h('dt',null,'配置状态'),h('dd',null,selectedEnvironment.restart_pending?'待重启生效':'配置已保存')),
        h('div',{className:'lm-destinations'},destination('账号管理',selectedEnvironment.account_count+' 条账号 · 独立网站会话',()=>goto('accounts',selectedEnvironment.env_id)),destination('环境设置','基本信息、网络代理与浏览器设置',()=>begin(selectedEnvironment)),destination('指纹信息','浏览器、设备、语言、时区、字体与网络落地',()=>openFingerprint(selectedEnvironment)),destination('历史版本','查看修改并恢复先前配置',()=>run('history',async()=>setVersions({env:selectedEnvironment,items:(await request('history',{env_id:selectedEnvironment.env_id})).versions}))),destination('复制配置','新建独立环境，保留来源环境',()=>begin(selectedEnvironment,true))),
        h('details',{className:'lm-section'},h('summary',null,'更多维护操作'),h('p',{className:'lm-muted'},'移出列表会保留账号和网站数据，可以从配置备份找回。'),btn('移出列表，保留网站数据',()=>run('remove',async()=>{if(!confirm('移出此环境？账号和网站数据继续留在本机，可从配置备份找回。'))return;await request('remove',{env_id:selectedEnvironment.env_id,revision:selectedEnvironment.revision});setSelected(old=>old.filter(x=>x!==selectedEnvironment.env_id));setEnvDetail('');await refresh();setNotice('已移出列表，账号及网站数据保留');}),false,selectedEnvironment.env_id===state.active)))
        :h('p',{className:'lm-empty'},'此环境已移出列表，请返回重新选择。');
      const destination=(title,description,fn)=>h('button',{type:'button',className:'lm-destination','aria-label':title,onClick:()=>{rememberScroll();fn();},disabled:!!busy},h('span',null,h('strong',null,title),h('small',null,description)),h('span',{'aria-hidden':true},'›'));
      const envList=()=>h('section',{className:'lm-enter'},
        h('div',{className:'lm-toolbar'},h('label',{className:'lm-search'},h('span',{className:'lm-sr'},'搜索环境'),h('input',{type:'search',value:query,placeholder:'搜索环境、平台、账号或备注',onChange:e=>updateView({query:e.target.value})})),btn(more?'收起筛选':'筛选',()=>setMore(!more)),select('排序',sort,v=>updateView({sort:v}),[['recent','最近使用'],['updated','最近编辑'],['name','名称']])),
        more&&h('div',{className:'lm-grid lm-section'},select('分组',group,v=>updateView({group:v}),[['','全部分组'],...groups.map(g=>[g,g])]),select('代理检测',proxyFilter,v=>updateView({proxy:v}),[['','全部'],['failed','检测异常'],['untested','未检测或已过期']]),select('账号资料',accountFilter,v=>updateView({account:v}),[['','全部'],['yes','已保存账号'],['no','尚未添加账号']]),btn('清除筛选',()=>setView({sort:'recent'}))),
        selected.length>0&&h('section',{className:'lm-selection'},h('strong',null,'已选 '+selected.length+' 个（跨页保留）'),h('div',{className:'lm-actions'},btn('批量操作',()=>setBulkDialog(true)),btn('取消选择',()=>setSelected([])))),
        h('div',{className:'lm-head'},h('label',{className:'lm-check'},h('input',{type:'checkbox',checked:visible.length>0&&visible.every(e=>selected.includes(e.env_id)),onChange:e=>setSelected(old=>e.target.checked?[...new Set([...old,...visible.map(e=>e.env_id)])]:old.filter(id=>!visible.some(e=>e.env_id===id)))}),'选择本页'),h('small',null,'找到 '+shown.length+' 个'),btn('选择全部筛选结果',()=>setSelected(shown.map(e=>e.env_id)))),
        h('div',{className:'lm-list'},...visible.map(e=>h('article',{key:e.env_id,className:'lm-row','data-active':e.env_id===actualActive,'data-env':e.env_id},
          h('input',{type:'checkbox','aria-label':'选择 '+e.display_name,checked:selected.includes(e.env_id),onChange:v=>setSelected(old=>v.target.checked?[...old,e.env_id]:old.filter(id=>id!==e.env_id))}),
          h('div',{className:'lm-identity'},environmentIcon(18),h('div',{className:'lm-name',title:e.display_name},e.display_name),h('small',null,(e.display_name===e.env_id?'':e.env_id+' · ')+(e.group?e.group+' · ':'')+(e.account_count||0)+' 个账号'),h('span',{className:'lm-badge lan-current-badge','data-current':e.env_id===actualActive},'当前')),
          h('div',{className:'lm-status'},h(RuntimeIndicator,{status:runtime.states[e.env_id]}),h('small',null,e.restart_pending?'待重启':status(e))),
          h('div',{className:'lm-actions lm-row-actions'},btn(e.env_id===actualActive?'回到网页':'打开环境',()=>run('open',async()=>{await request('open',{env_id:e.env_id});runtimeStatus.setActive(e.env_id,e.selected_account_session||e.env_id);}),true),btn('管理',()=>manageEnvironment(e))))),!shown.length&&h('div',{className:'lm-empty'},state.loading?'正在读取环境…':'没有匹配环境。可以清除筛选或新建环境。')),
        h('footer',{className:'lm-footer'},select('每页',String(size),v=>{setSize(Number(v));setPage(0);},[['25','25 个'],['50','50 个'],['100','100 个']]),h('div',{className:'lm-actions'},btn('上一页',()=>setPage(actualPage-1),false,actualPage===0),h('span',null,(actualPage+1)+' / '+Math.max(1,Math.ceil(shown.length/size))),btn('下一页',()=>setPage(actualPage+1),false,(actualPage+1)*size>=shown.length))));
      const beginAccount=a=>{setAccountDetail('');setAccountEditing(true);setDrafts(old=>a?(old.some(d=>d.account_id===a.account_id)?old:[...old,{...a,client_id:a.account_id,password:'',otp_secret:''}]):[...old,freshAccount()]);};
      const closeAccountEditor=()=>run('account-draft',async()=>{await flushDraft();setAccountEditing(false);setSavedDraft(draftValue());});
      const accountPage=()=>h('section',{className:'lm-enter'},envPicker,h('div',{className:'lm-head'},h('p',{className:'lm-muted'},'账号独立保存，同平台账号使用独立网站会话。'),btn('＋ 添加账号',()=>beginAccount(),true)),
        !accountEditing&&drafts.length>0&&h('div',{className:'lm-notice'},'有 '+drafts.length+' 条账号尚未保存',btn('继续编辑账号',()=>setAccountEditing(true))),
        ...accounts.map(a=>h('article',{key:a.account_id,className:'lm-account-card','data-account':a.account_id},h('div',{className:'lm-head'},h('div',null,h('strong',{className:'lm-icon-title'},platformIcon(a,20),a.platform+' · '+(a.username||a.login)),h('small',{className:'lm-account-login-row'},h('span',{className:'lm-account-login-value'},a.login+(a.site?' · '+a.site:'')),h('button',{type:'button',className:'lm-copy-login','aria-label':'复制 '+a.platform+' 登录账号',onClick:()=>run('copy-login',async()=>{await copyLocalCredential(a.login);setNotice('登录账号已复制');})},'复制'))),h('span',{className:'lm-badge'},accountStatus[a.status]||'登录状态待检查')),h('p',{className:'lm-muted'},a.label||a.url||'旧版账号：请补充网站地址'),h('div',{className:'lm-actions'},btn('打开账号网站',()=>accountAction(a,'open'),true),btn('账号详情',()=>{setAccountDetail(a.account_id);})))),
        !accounts.length&&!drafts.length&&h('p',{className:'lm-empty'},'此环境还没有账号。选择平台后可以连续添加。'));
      const activeAccount=accounts.find(a=>a.account_id===accountDetail);
      const accountDetails=()=>activeAccount&&h(WorkspaceDialog,{title:'账号详情',context:'所属环境：'+(current?.display_name||focusEnv)+' · '+focusEnv,onClose:()=>{setAccountDetail('');},locked:!!busy,footer:h('div',{className:'lm-actions'},btn('编辑账号资料',()=>beginAccount(activeAccount),true))},
        h('h3',{className:'lm-icon-title'},platformIcon(activeAccount,20),activeAccount.platform+' · '+(activeAccount.username||activeAccount.login)),h('dl',null,h('dt',null,'登录账号'),h('dd',{className:'lm-login-detail'},h('span',{className:'lm-login-value'},activeAccount.login),h('button',{type:'button',className:'lm-copy-login',onClick:()=>run('copy-login',async()=>{await copyLocalCredential(activeAccount.login);setNotice('登录账号已复制');})},'复制')),h('dt',null,'网站'),h('dd',null,activeAccount.url||'尚未设置'),h('dt',null,'备注'),h('dd',null,activeAccount.label||'—'),h('dt',null,'登录状态'),h('dd',null,accountStatus[activeAccount.status]||'待检查')),
        notice&&h('p',{role:'status',className:'lm-notice'},notice),h('section',{className:'lm-section'},h('h3',null,'登录操作'),h('label',{className:'lm-field'},h('span',null,'本次验证码'),h('input',{type:'text',inputMode:'numeric',autoComplete:'one-time-code',value:manualCodes[activeAccount.account_id]||'',placeholder:'需要时填写，不长期保存',onChange:e=>setManualCodes({...manualCodes,[activeAccount.account_id]:e.target.value})})),h('div',{className:'lm-actions'},btn('填入登录资料',()=>accountAction(activeAccount,'fill')),btn('检查登录状态',()=>accountAction(activeAccount,'check')),btn('我已确认登录',()=>run('confirm-login',async()=>{if(!confirm('请在网站确认显示的账号正确且已登录，再记录此状态。'))return;const r=await request('accounts',{env_id:focusEnv,account_id:activeAccount.account_id,action:'confirm'});setNotice(r.message);await loadAccounts(focusEnv);}),false,activeAccount.status==='user_confirmed'))),
        h(AccountCredentials,{key:focusEnv+':'+activeAccount.account_id+':'+activeAccount.revision,account:activeAccount,envId:focusEnv}),
        h('details',{className:'lm-section'},h('summary',null,'移除账号'),h('p',{className:'lm-muted'},'只移除账号资料，保留网站登录数据。'),btn('移除账号资料',()=>run('account-delete',async()=>{if(!confirm('仅移除这条账号资料，网站登录数据仍保留，继续？'))return;await request('accounts',{action:'delete',env_id:focusEnv,account_id:activeAccount.account_id,revision:activeAccount.revision});setAccountDetail('');await loadAccounts(focusEnv);await refresh();}))));
      const jobTitle=job?({running:'检测中',cancelling:'取消中',complete:'检测完成',cancelled:'已取消',interrupted:'检测中断'}[job.status]||job.status)+' · '+job.done+'/'+job.total:'';
      const proxyPage=()=>h('section',{className:'lm-enter'},
        h('div',{className:'lm-head'},h('div',null,h('h3',null,proxyDetail?'检测结果':'代理检测'),h('small',null,'HTTPS 请求往返延迟；城市与时区按出口 IP 查询。结果 15 分钟后标为历史。')),
          btn('测试全部代理',()=>startJob(state.envs.filter(e=>!['','direct','system'].includes(e.proxy)).map(e=>e.env_id)),true,!state.envs.some(e=>!['','direct','system'].includes(e.proxy)))),
        job&&h('section',{className:'lm-section'},h('h3',null,jobTitle),h('progress',{className:'lm-progress',value:job.done,max:job.total}),
          !proxyDetail&&btn('查看检测结果',()=>setProxyDetail(true)),
          proxyDetail&&h(React.Fragment,null,h('p',{className:'lm-muted'},'操作范围：'+job.env_ids.join('、')),h('div',{className:'lm-actions'},['running','cancelling'].includes(job.status)&&btn('取消后续检测',()=>run('cancel',async()=>setJob((await request('proxy-job',{action:'cancel',job_id:job.id})).job))),btn(jobFilter?'显示全部':'只看异常',()=>setJobFilter(!jobFilter)),!['running','cancelling'].includes(job.status)&&btn('只重测异常',()=>startJob(job.results.filter(r=>['failed','mismatch'].includes(r.status)).map(r=>r.env_id)),false,!job.results.some(r=>['failed','mismatch'].includes(r.status)))),
            ...job.results.filter(r=>!jobFilter||['failed','mismatch'].includes(r.status)).map(r=>h('div',{className:'lm-result',key:r.env_id},h('strong',null,state.envs.find(e=>e.env_id===r.env_id)?.display_name||r.env_id),' · ',r.status==='success'?'通过':r.status==='mismatch'?'出口不符':r.error||r.status,
              r.exit_ip&&h('small',{className:'lm-proxy-facts'},'出口 IP '+r.exit_ip+' · 延迟 '+(r.latency_ms==null?'—':r.latency_ms+' ms')+' · '+[r.city,r.region,r.country].filter(Boolean).join('、')+' · '+(r.timezone||'时区未知')+' · '+(r.org||r.asn||'运营商未知')+(r.geo_error?' · '+r.geo_error:'')),btn('修改设置',()=>begin(state.envs.find(e=>e.env_id===r.env_id))))))),
        !proxyDetail&&state.envs.map(e=>h('div',{className:'lm-result',key:e.env_id},h('strong',null,e.display_name),' · '+status(e)+' · '+dateLabel(e.proxy_test?.at),btn('检测',()=>startJob([e.env_id])))));
      const dataPage=()=>h('section',{className:'lm-enter'},
        !dataSection&&h(React.Fragment,null,h('p',{className:'lm-muted'},'选择要维护的数据；账号与网站登录数据均保存在本机。'),h('div',{className:'lm-destinations'},destination('环境配置备份','导出设置、查看备份与选择性恢复',()=>setDataSection('config')),destination('账号加密迁移','用迁移密码保护账号资料和 2FA 密钥',()=>setDataSection('portable')),destination('配置检查和操作记录','检查配置提醒、查看最近维护记录',()=>setDataSection('activity')))),
        dataSection==='config'&&h('section',{className:'lm-section'},h('h3',null,'环境配置备份'),h('p',null,'保存环境设置，不包含账号密码、Cookie 和网站登录状态。恢复设置会保留本机现有网站数据。'),h('div',{className:'lm-actions'},btn('备份并导出配置',()=>run('backup',async()=>{download((await request('backup')).document,'蓝豆环境配置-'+Date.now()+'.json');await loadMaintenance();}),true),file('选择配置备份文件',async document=>{const r=await request('restore',{document});setRestore(r);setRestoreSource({document});setRestoreIds([]);})),
          !(maintenance?.backups||[]).length&&h('p',{className:'lm-empty'},'尚无配置备份。可以先创建一份备份。'),
          ...(maintenance?.backups||[]).slice(0,20).map(b=>h('div',{className:'lm-result',key:b.id},dateLabel(b.created_at)+' · '+b.count+' 个环境 · '+({'manual':'手动备份','before-remove':'移除前','before-restore':'恢复前'}[b.reason]||'配置备份'),btn('查看恢复内容',()=>run('preview',async()=>{setRestore(await request('restore',{backup_id:b.id}));setRestoreSource({backup_id:b.id});setRestoreIds([]);}))))),
        dataSection==='portable'&&h('section',{className:'lm-section'},h('h3',null,'账号加密迁移包'),h('p',null,'迁移账号资料和密钥，不包含网站登录数据。先恢复环境配置，再导入对应账号。'),h('label',{className:'lm-field'},h('span',null,'迁移密码（至少 10 位）'),h('input',{name:'transfer-password',type:'password',autoComplete:'new-password',value:transferPassword,onChange:e=>{setTransferPassword(e.target.value);setTransferPreview(null);}})),
          h('div',{className:'lm-actions'},btn('导出加密账号包',()=>run('portable',async()=>{download((await request('portable',{action:'export',password:transferPassword})).document,'蓝豆账号加密包-'+Date.now()+'.json');setTransferPassword('');})),file('选择加密账号包',async document=>{setTransfer(document);setTransferPreview(null);}),transfer&&btn('检查迁移包',()=>run('portable-preview',async()=>setTransferPreview(await request('portable',{action:'preview',password:transferPassword,document:transfer}))))),
          transferPreview&&h('div',{className:'lm-notice'},...transferPreview.environments.map(e=>h('p',{key:e.env_id},e.env_id+' · '+e.count+' 条账号 · '+(e.exists?'可以导入':'请先恢复此环境配置'))),btn('导入到对应环境',()=>run('portable-import',async()=>{const r=await request('portable',{action:'import',password:transferPassword,document:transfer});setResults(r.results);setTransferPassword('');setTransferPreview(null);await refresh();}),true))),
        dataSection==='activity'&&h('section',{className:'lm-section'},h('h3',null,'配置检查和操作记录'),!(maintenance?.issues||[]).length&&h('p',null,'未发现需要提醒的配置项'),...(maintenance?.issues||[]).map((i,n)=>h('p',{key:n},i.message+' · '+i.env_ids?.join('、'))),h('h3',null,'最近操作'),...(maintenance?.events||[]).slice(0,100).map(e=>h('p',{key:e.id},dateLabel(e.at)+' · '+({create:'新建环境',edit:'修改配置',account_save:'保存账号',account_clear:'移除账号',backup:'配置备份',restore:'恢复配置',proxy_test:'代理检测',proxy_inspection:'批量检测'}[e.action]||'维护操作')+' · '+(e.env_id||'')))));
      const resumeDraft=()=>{if(!savedDraft)return;rememberScroll();setDraftPrompt(false);setEnvDetail('');setDataSection('');setProxyDetail(false);setForm(savedDraft.form);setInitial(savedDraft.initial);setEditing(savedDraft.editing);setDrafts(savedDraft.drafts||[]);setFocusEnv(savedDraft.focusEnv||focusEnv);setTab(savedDraft.tab||'envs');editorReturn.current=savedDraft.editorReturn||{tab:'envs',envDetail:'',dataSection:'',proxyDetail:false};setEditorSection(savedDraft.editorSection||'basic');setAccountEditing(!savedDraft.form&&!!savedDraft.drafts?.length);setSavedDraft(null);};
      const discardDraftAndBegin=()=>run('discard-draft',async()=>{clearTimeout(draftTimer.current);await enqueueDraft('clear');setSavedDraft(null);setDraftPrompt(false);beginEditor();});
      const back=()=>run('back',async()=>{rememberScroll();setNotice('');setResults([]);if(form){await flushDraft();setSavedDraft(draftValue());setForm(null);setDrafts([]);const origin=editorReturn.current||{tab:'envs',envDetail:'',dataSection:'',proxyDetail:false};setTab(origin.tab);setEnvDetail(origin.envDetail);setDataSection(origin.dataSection);setProxyDetail(origin.proxyDetail);}else if(fingerprintEnv){setFingerprintEnv('');setFingerprintData(null);}else if(envDetail){setEnvDetail('');}else if(dataSection){setDataSection('');setTransferPassword('');setTransferPreview(null);}else if(proxyDetail){setProxyDetail(false);}else if(tab==='imports'){setTab('envs');await refresh();}});
      const isSecondary=!!form||!!fingerprintEnv||!!envDetail||!!dataSection||proxyDetail||tab==='imports';
      const pageTitle=form?(editing?'编辑环境':'新建环境'):fingerprintEnv?'指纹信息':envDetail?'环境详情':dataSection?({config:'环境配置备份',portable:'账号加密迁移',activity:'配置检查和操作记录'}[dataSection]):proxyDetail?'代理检测结果':tab==='imports'?'批量新建 / 导入':'我的环境';
      const modalAccountEditor=accountEditing&&drafts.length>0&&!form;
      // A saved draft is actionable from the workspace home. It must not float
      // over read-only detail pages such as fingerprint information.
      return h('div',{ref:panel,className:'lan-manager lm-workspace','data-workspace-version':'6'},h('style',null,CSS+WORKSPACE_CSS),
        h('header',{className:'lm-shell-header'},isSecondary&&h('nav',{className:'lm-breadcrumb','aria-label':'当前位置'},btn('我的环境',()=>goto('envs')),h('span',{'aria-hidden':true},'/'),h('span',{'aria-current':'page'},pageTitle)),
          h('div',{className:'lm-head'},h('div',null,h('h2',null,pageTitle),h('small',null,form?'分类填写设置，输入会保留':state.envs.length+' 个环境 · 本机保存')),h('div',{className:'lm-actions'},isSecondary&&btn('返回上一级',back),btn('刷新',()=>run('refresh',async()=>{await refresh();if(tab==='data')await loadMaintenance();})),!isSecondary&&btn('批量新建 / 导入',()=>goto('imports')),!isSecondary&&btn('＋ 新建环境',beginNewEnvironment,true)))),
        !isSecondary&&h('nav',{className:'lm-tabs','aria-label':'环境工作区'},...[['envs','环境'],['accounts','账号'],['proxy','代理检测'],['data','备份与恢复']].map(([key,label])=>h('button',{type:'button',key,role:'tab','aria-selected':tab===key,onClick:()=>goto(key)},label))),
        h('main',{ref:content,className:'lm-content',tabIndex:0,onScroll:rememberScroll,'aria-label':'我的环境内容'},
          h('div',{className:'lm-toast-layer',hidden:tab==='imports'&&!form,'aria-live':'polite'},
            (state.error||state.warning||state.account_warning)&&h('div',{className:'lm-notice lm-connection lm-toast',role:'status'},state.error||state.warning||state.account_warning,btn('重新连接',()=>run('connect',refresh))),
            notice&&h('div',{className:'lm-notice lm-toast',role:'status'},notice),busy&&h('div',{className:'lm-busy lm-toast',role:'status'},'正在处理…'),
          ),
          h(BatchImportPage,{hidden:tab!=='imports'||!!form,envs:state.envs,platforms,workspaceNotice:notice,connectionWarning:state.error||state.warning||state.account_warning,onChanged:()=>{refresh();runtimeStatus.invalidate();window.dispatchEvent(new Event('lan-env-imported'));}}),resultView,form?editor():tab==='imports'?null:tab==='envs'?(fingerprintEnv?fingerprintPage():envDetail?environmentPage():envList()):tab==='accounts'?accountPage():tab==='proxy'?proxyPage():dataPage()),
        modalAccountEditor&&h(WorkspaceDialog,{title:'编辑平台账号',context:'所属环境：'+(current?.display_name||focusEnv)+' · '+focusEnv,onClose:closeAccountEditor,locked:!!busy,footer:h('div',{className:'lm-actions'},btn('稍后继续',closeAccountEditor),btn('保存全部账号',()=>run('accounts-save',async()=>{if(await saveAccounts(focusEnv)){clearTimeout(draftTimer.current);await enqueueDraft('clear');setSavedDraft(null);setAccountEditing(false);}await refresh();}),true))},notice&&h('p',{role:'status',className:'lm-notice'},notice),resultView,accountEditor),
        accountDetails(),
        draftPrompt&&h(WorkspaceDialog,{title:'发现未完成草稿',context:'之前填写的环境或账号设置尚未保存。请选择如何处理。',onClose:()=>setDraftPrompt(false),locked:!!busy,footer:h('div',{className:'lm-actions'},btn('取消',()=>setDraftPrompt(false)),btn('继续编辑草稿',resumeDraft,true),btn('放弃草稿并新建',discardDraftAndBegin))},h('p',null,'继续编辑会恢复上次填写内容；放弃草稿会清除未保存内容并打开空白的新建环境页面。')),
        bulkDialog&&h(WorkspaceDialog,{title:'批量操作',onClose:()=>setBulkDialog(false),locked:!!busy},h('p',null,'已选 '+selected.length+' 个环境，跨页选择保留'),h('details',null,h('summary',null,'查看操作范围'),h('p',{className:'lm-muted'},selected.join('、'))),resultView,
          h('section',{className:'lm-section'},h('h3',null,'批量分组'),h('label',{className:'lm-field'},h('span',null,'新分组'),h('input',{placeholder:'新分组',value:bulkGroup,onChange:e=>setBulkGroup(e.target.value)})),btn('设置分组',()=>run('bulk',async()=>{const r=await request('bulk',{env_ids:selected,fields:{group:bulkGroup}});setResults(r.results);await refresh();}))),
          h('section',{className:'lm-section'},h('h3',null,'批量标签'),h('label',{className:'lm-field'},h('span',null,'标签（逗号分隔）'),h('input',{placeholder:'新标签，逗号分隔',value:bulkTags,onChange:e=>setBulkTags(e.target.value)})),btn('设置标签',()=>run('bulk',async()=>{const r=await request('bulk',{env_ids:selected,fields:{tags:bulkTags}});setResults(r.results);await refresh();}))),
          h('section',{className:'lm-section'},h('h3',null,'网络检测'),btn('检测所选代理',()=>{setBulkDialog(false);startJob(selected);}))),
        versions&&h(WorkspaceDialog,{title:'历史版本',onClose:()=>setVersions(null),locked:!!busy},h('h3',null,versions.env.display_name),notice&&h('p',{role:'status'},notice),!versions.items.length&&h('p',null,'暂无历史修改记录'),...versions.items.map(v=>h('div',{className:'lm-result',key:v.id},dateLabel(v.at),h('details',null,h('summary',null,'查看此版本设置'),h('dl',null,...Object.entries(v.config).flatMap(([k,val])=>[h('dt',{key:k},LABELS[k]||k),h('dd',{key:k+'v'},val||'空')]))),btn('恢复此版本',()=>run('history-restore',async()=>{if(!confirm('恢复此版本配置？账号与网站数据保留。'))return;await request('history',{action:'restore',env_id:versions.env.env_id,version_id:v.id,revision:versions.env.revision});setVersions(null);await refresh();setNotice('已恢复配置版本，网络或身份设置需重启生效');}))))),
        restore&&h(WorkspaceDialog,{title:'确认恢复内容',onClose:()=>setRestore(null),locked:!!busy,footer:btn('恢复所选环境',()=>run('restore',async()=>{const r=await request('restore',{...restoreSource,action:'apply',preview_token:restore.preview_token,env_ids:restoreIds});setResults(r.results);await refresh();await loadMaintenance();if(r.ok)setRestore(null);else{const next=await request('restore',restoreSource);setRestore(next);setRestoreIds(next.changes.filter(c=>restoreIds.includes(c.env_id)).map(c=>c.env_id));}setNotice(r.ok?'配置恢复完成；账号和网站数据保留，配置变更需重启生效':'部分恢复未完成，剩余项目已重新预览。');}),true,!restoreIds.length)},h('p',null,'仅更新勾选环境，账号和网站数据保留。'),notice&&h('p',{role:'status',className:'lm-notice'},notice),resultView,!restore.changes.length&&h('p',null,'备份与当前配置一致，无需恢复。'),...restore.changes.map(c=>h('div',{className:'lm-result',key:c.env_id},h('label',{className:'lm-check'},h('input',{type:'checkbox',checked:restoreIds.includes(c.env_id),onChange:e=>setRestoreIds(old=>e.target.checked?[...old,c.env_id]:old.filter(x=>x!==c.env_id))}),c.display_name+' · '+(c.action==='create'?'找回 / 新增':'更新配置')),h('dl',null,...c.fields.flatMap(k=>[h('dt',{key:k},LABELS[k]||k),h('dd',{key:k+'v'},(c.before?.[k]||'空')+' → '+(c.after?.[k]||'空'))]))))));
    }
    const WORKSPACE_CSS=`
      .lm-import-form{border:0;padding:0;margin:0;min-width:0}
      .lm-import-page[hidden]{display:none!important}
      .lm-import-page{animation:lm-enter .15s ease-out}
      .lm-import-page .lm-subnav{flex-wrap:wrap}
      .lm-import-page .lm-check{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;flex:none}
      .lm-import-page input[type=radio]{width:16px;height:16px;flex:none;padding:0;margin:0;accent-color:#2874db}
      .lm-import-history-toggle{margin-left:auto}
      .lm-import-result-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}
      .lm-import-result-tools input{flex:1;min-width:130px;width:auto}
      .lm-import-result-tools select{width:auto;max-width:150px}
      .lm-import-result-tools small{white-space:nowrap}
      .lm-import-history-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;padding:10px 0;border-bottom:1px solid var(--dsw-alias-border-l2,#e4e8ee);align-items:center;font-size:12px}
      .lm-import-history-row small{display:block;margin-top:4px;color:var(--dsw-alias-text-secondary,#687386);overflow-wrap:anywhere}
      @media(max-width:760px){.lm-import-history-row{grid-template-columns:minmax(0,1fr)}.lm-import-history-row .lm-actions{justify-content:flex-end}}
      .lm-import-feedback{height:54px;box-sizing:border-box;overflow:auto;line-height:18px;padding:8px 0;color:var(--dsw-alias-text-secondary,#687386);font-size:12px}
      .lm-import-page textarea{width:100%;box-sizing:border-box;resize:vertical;min-height:80px}
      .lm-import-page progress{width:100%;height:7px;accent-color:#2874db;transition:opacity .15s}
      .lm-import-rows{border:1px solid var(--dsw-alias-border-l2,#dce2e9);border-radius:8px;overflow:hidden}
      .lm-import-row{display:grid;grid-template-columns:30px minmax(0,1fr) 56px;gap:8px;align-items:start;padding:8px 10px;border-bottom:1px solid var(--dsw-alias-border-l2,#e7ebf0);font-size:12px}
      .lm-import-row:last-child{border-bottom:0}.lm-import-row strong,.lm-import-row small{display:block;overflow-wrap:anywhere}
      .lm-import-row small{margin-top:3px;color:var(--dsw-alias-text-secondary,#687386)}.lm-import-row p{margin:4px 0 0}
      .lm-import-row[data-status=success]>span:last-child{color:#16834b}.lm-import-row[data-status=invalid]>span:last-child,.lm-import-row[data-status=failed]>span:last-child{color:#bc3e42}
      .lm-import-reference{max-height:160px;overflow:auto;font-size:12px;line-height:1.8}
      @media(prefers-reduced-motion:reduce){.lm-import-page{animation:none}}

      .lan-manager.lm-workspace{max-width:none;font-size:14px;padding:0;flex:1;min-width:0;min-height:0;height:100%;display:flex;flex-direction:column;overflow:hidden;--lm-duration:160ms;--lm-motion:cubic-bezier(.2,.8,.2,1)}
      .lm-shell-header{flex:none;padding:12px 20px 8px;border-bottom:1px solid var(--lm-border);background:var(--lm-bg)}
      .lm-shell-header .lm-head{margin:0}.lm-shell-header h2{overflow-wrap:anywhere}
      .lan-manager .lm-tabs{flex:none;margin:0;padding:6px 20px;overflow-x:auto;white-space:nowrap}.lm-tabs button{flex-shrink:0}
      .lm-content{position:relative;flex:1;min-height:0;min-width:0;overflow-y:auto;overscroll-behavior-y:contain;scrollbar-gutter:stable;padding:6px 20px 16px;scroll-padding-block:16px;outline-offset:-2px}
      .lm-connection{display:flex;align-items:center;justify-content:space-between;gap:10px}.lan-manager .lm-connection{padding:8px 12px}.lm-connection button{padding:2px 6px;flex-shrink:0}.lm-content form>.lm-section{border-top:0}.lm-content:focus-visible{outline:2px solid var(--lm-blue)}
      .lm-content::-webkit-scrollbar,.lm-modal-body::-webkit-scrollbar{display:block;width:10px}.lm-content::-webkit-scrollbar-thumb,.lm-modal-body::-webkit-scrollbar-thumb{background:#a8afbb;border:2px solid var(--lm-bg);border-radius:6px}
      .lm-breadcrumb{display:flex;gap:8px;align-items:center;margin-bottom:6px;font-size:13px}.lm-breadcrumb button{padding:2px 0;border:0;color:var(--lm-blue)}
      .lm-subnav{display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid var(--lm-border);padding:10px 0 16px}.lm-subnav [aria-current=page]{color:var(--lm-blue);border-color:var(--lm-blue);background:rgba(35,103,223,.06)}
      .lm-destinations{display:grid;margin:16px 0}.lan-manager .lm-destination{width:100%;border-width:0 0 1px;border-radius:0;padding:18px 4px;display:flex;align-items:center;justify-content:space-between;text-align:left;gap:16px}.lm-destination strong,.lm-destination small{display:block}.lm-destination small{margin-top:5px}.lm-destination>span:last-child{font-size:24px;color:var(--lm-muted)}
      .lm-selection{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px;border:1px solid var(--lm-border);margin:12px 0;border-radius:7px}
      .lm-workspace .lm-toolbar{display:flex;gap:8px;align-items:end;margin:8px 0}.lm-search{flex:1}.lm-toolbar .lm-field{min-width:130px}
      .lm-workspace .lm-list{border-top:1px solid var(--lm-border)}.lm-workspace .lm-row{grid-template-columns:18px minmax(0,1fr) minmax(190px,.55fr) auto;gap:16px;padding:2px 0;min-height:38px}
      .lm-workspace .lm-row-actions{gap:6px;justify-content:flex-end}.lm-workspace .lm-row-actions button{padding:3px 8px;min-height:27px;line-height:1.1;font-size:12px}.lm-workspace .lm-status{display:flex;align-items:center;justify-content:flex-start;gap:8px;white-space:nowrap}.lm-status small{display:inline;font-size:11px;line-height:1.1}.lm-badge{display:inline-block;margin-left:8px;padding:1px 6px;border-radius:4px;background:rgba(35,103,223,.08);color:var(--lm-blue);font-size:11px;font-weight:normal}
      .lm-workspace .lm-identity small{display:inline-block;margin:0 0 0 10px;font-size:11px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:middle}
      .lm-workspace .lm-identity{display:grid;grid-template-columns:18px minmax(0,1fr) minmax(0,96px) 38px;gap:6px;align-items:center;min-width:0}.lm-workspace .lm-name{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.lm-workspace .lm-identity small{min-width:0;margin:0}.lm-workspace .lm-identity>.lan-current-badge{box-sizing:border-box;width:38px;margin:0;text-align:center;justify-self:end}
      .lm-identity>img[data-platform-icon]{margin:0}.lm-icon-title{display:flex;align-items:center;gap:7px;min-width:0}.lm-platform-control{display:flex;align-items:center;gap:8px;min-width:0}.lm-platform-control select{flex:1;min-width:0}
      @container lm-panel (max-width:850px){.lm-workspace .lm-identity{grid-template-columns:18px minmax(0,1fr) 38px}.lm-workspace .lm-identity small{display:none}}
      .lm-workspace .lm-head{margin-bottom:8px}
      .lm-detail{grid-column:2/-1;background:rgba(100,120,150,.04);padding:14px;border-radius:7px}.lm-inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}.lm-inline input{flex:1;min-width:120px}.lm-inline select{width:auto}
      .lm-account-card,.lm-account-edit{border:1px solid var(--lm-border);border-radius:6px;padding:12px;margin:8px 0}
      .lm-account-card small{display:block;margin-top:4px;overflow-wrap:anywhere;word-break:break-word}.lm-account-login-row{display:flex!important;align-items:flex-start;gap:8px}.lm-account-login-value{min-width:0;flex:1;overflow-wrap:anywhere;word-break:break-word}.lm-account-edit{border-left:3px solid var(--lm-blue)}
      .lm-empty{padding:40px 12px;text-align:center}.lm-file{display:inline-flex;gap:10px;align-items:center;margin:10px 0}.lm-workspace .lm-file input{width:auto;max-width:280px}
      .lm-modal{width:min(760px,calc(100% - 40px));max-width:none;max-height:calc(100dvh - 40px);margin:auto;padding:0;overflow:hidden;color:inherit;background:var(--lm-bg);border:1px solid var(--lm-border);box-shadow:0 12px 55px #0003;border-radius:12px}
      .lm-modal[open]{display:flex;flex-direction:column;animation:lm-enter var(--lm-duration) var(--lm-motion)}.lm-modal::backdrop{background:rgba(15,23,42,.28);animation:lm-backdrop var(--lm-duration) var(--lm-motion)}
      .lm-modal-head,.lm-modal-foot{flex:none;padding:10px 16px;background:var(--lm-bg)}.lm-modal-head{display:flex;justify-content:space-between;align-items:center;gap:16px;border-bottom:1px solid var(--lm-border)}.lm-modal-head h3{margin:0}.lm-modal-head small{display:block;margin-top:4px;overflow-wrap:anywhere}.lm-modal-foot{border-top:1px solid var(--lm-border)}.lm-modal-foot .lm-actions{justify-content:flex-end}
      .lm-modal-body{flex:1;min-height:0;overflow-y:auto;overscroll-behavior-y:contain;scrollbar-gutter:stable;padding:12px 16px;scroll-padding-block:12px}.lm-modal-body .lm-account-edit:first-child{margin-top:0}.lm-login-detail{display:flex;align-items:flex-start;gap:10px}.lm-login-value{min-width:0;overflow-wrap:anywhere;word-break:break-word}.lm-copy-login{flex:none;border:0;background:none;color:var(--lm-blue);padding:0;cursor:pointer;font:inherit}.lm-copy-login:focus-visible{outline:3px solid #8bb9ff;outline-offset:2px}
      @keyframes lm-backdrop{from{opacity:0}to{opacity:1}}
      .lm-credential-row{display:flex;align-items:center;flex-wrap:wrap;gap:10px;min-height:38px}.lm-password{flex:1;min-width:70px;overflow-wrap:anywhere;user-select:text}.lan-manager .lm-otp-code{font:600 24px/1.3 Consolas,monospace;letter-spacing:3px;color:var(--lm-blue);border:0;background:transparent;font-variant-numeric:tabular-nums}.lm-secret{font-size:24px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.lm-busy{position:sticky;top:0;background:var(--lm-bg);padding:8px;border-left:3px solid var(--lm-blue);z-index:5}
      .lm-proxy-facts{display:block;margin:3px 0;color:var(--lm-muted)}
      .lm-toast-layer[hidden]{display:none}
      .lm-toast-layer{position:absolute;z-index:20;top:auto;bottom:12px;left:20px;right:20px;max-height:35%;overflow-y:auto;display:grid;align-content:end;gap:6px;pointer-events:none}.lm-toast{pointer-events:auto;justify-self:end;max-width:min(620px,100%);padding:7px 10px!important;margin:0!important;background:var(--lm-bg);box-shadow:0 3px 12px #0002}.lm-toast-layer .lm-busy{position:static;justify-self:end;padding:6px 10px}
      .lm-workspace button{transition:background var(--lm-duration),transform 100ms}.lm-workspace button:active:not(:disabled){transform:translateY(1px)}.lm-workspace button:disabled{cursor:not-allowed}
      .lm-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
      @container lm-panel (max-width:850px){.lm-row{grid-template-columns:18px minmax(0,1fr) minmax(100px,.65fr) auto;gap:6px;padding:2px 0;min-height:36px}.lm-status{grid-column:auto;grid-row:auto;min-width:0;overflow:hidden;text-overflow:ellipsis}.lm-row-actions{grid-column:auto;grid-row:auto;flex-direction:row;gap:4px}.lm-row-actions button{padding:3px 6px;min-height:26px;font-size:11px}.lm-toolbar{flex-wrap:wrap}.lm-search{flex-basis:100%}.lm-detail{grid-column:1/-1}}
      @container lm-panel (max-width:560px){.lm-shell-header{padding:16px 16px 12px}.lan-manager .lm-tabs{padding:10px 16px}.lm-content{padding:8px 16px 20px}.lm-shell-header h2{font-size:18px}}
      @media(max-width:600px){.lm-modal{width:calc(100% - 24px);max-height:calc(100dvh - 24px)}.lm-modal-body,.lm-modal-head,.lm-modal-foot{padding:14px 16px}.lm-modal .lm-grid{grid-template-columns:minmax(0,1fr)}}
      @media(prefers-reduced-motion:reduce){.lm-account-card,.lm-account-edit,.lm-modal[open],.lm-modal::backdrop{animation:none}.lm-workspace button{transition:none}}
    `;

    // The row's glyph: rendered inside the nav button, so it must be a small square icon.
    function LanGlyph({ size = 18 }) {
      return h(
        'svg',
        { width: size, height: size, viewBox: '0 0 20 20', 'aria-hidden': 'true',
          style: { display: 'block' } },
        h('path', {
          d: 'M3 4.5h14v9H3zM7 16.5h6',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinejoin: 'round',
        }),
      );
    }

    const scopeEvents = new EventTarget();

    // A session-scoped slot, never the currently visible browser environment.
    // The bridge persists the grant and checks it again for every browser step.
    function EnvScopePicker({ sessionId }) {
      const [data, setData] = React.useState(null);
      const [error, setError] = React.useState('');
      const [open, setOpen] = React.useState(false);
      const [busy, setBusy] = React.useState(false);
      const [query, setQuery] = React.useState('');
      const anchor = React.useRef(null);
      const menu = React.useRef(null);
      const live = React.useRef(true);
      const requestSeq = React.useRef(0);
      const refresh = React.useCallback(async () => {
        const seq = ++requestSeq.current;
        try {
          const reply = await call('/v1/lan/session-scope', { session_id: sessionId });
          if (reply.ok !== true) throw new Error(reply.error || '环境列表不可用');
          if (live.current && seq === requestSeq.current) { setData(reply); setError(''); }
        } catch (err) {
          if (live.current && seq === requestSeq.current) setError(String(err.message || err));
        }
      }, [sessionId]);
      React.useEffect(() => {
        live.current = true;
        refresh();
        const onChange = (event) => { if (event.detail === sessionId) refresh(); };
        scopeEvents.addEventListener('changed', onChange);
        return () => { live.current = false; scopeEvents.removeEventListener('changed', onChange); };
      }, [refresh, sessionId]);
      React.useEffect(() => {
        if (!open) return;
        refresh();
        const popup = menu.current;
        const place = () => {
          const rect = anchor.current?.getBoundingClientRect();
          if (!rect || !popup) return;
          const width = Math.min(320, Math.max(0, innerWidth - 24));
          const below = Math.max(0, innerHeight - rect.bottom - 18);
          const above = Math.max(0, rect.top - 18);
          Object.assign(popup.style, { width: `${width}px`,
            maxHeight: `${Math.min(420, Math.max(below, above))}px`,
            left: `${Math.max(12, Math.min(rect.left, innerWidth - width - 12))}px` });
          // Measure the shown popover, not its maximum height: filtered/short
          // lists should stay beside the trigger and long lists must fit.
          const height = popup.getBoundingClientRect().height;
          const top = below >= height || below >= above ? rect.bottom + 6 : rect.top - height - 6;
          popup.style.top = `${Math.max(12, Math.min(top, innerHeight - height - 12))}px`;
        };
        popup?.showPopover();
        place();
        popup?.querySelector('input[type="search"]')?.focus({ preventScroll: true });
        const resize = new ResizeObserver(place);
        if (popup) resize.observe(popup);
        if (anchor.current) resize.observe(anchor.current);
        const scroll = event => { if (!popup?.contains(event.target)) place(); };
        const outside = (event) => {
          if (!popup?.contains(event.target) && !anchor.current?.contains(event.target)) setOpen(false);
        };
        const escape = (event) => {
          if (event.key === 'Escape') { event.stopPropagation(); setOpen(false); anchor.current?.focus(); }
        };
        document.addEventListener('pointerdown', outside, true);
        document.addEventListener('keydown', escape, true);
        document.addEventListener('scroll', scroll, true);
        addEventListener('resize', place);
        return () => {
          resize.disconnect();
          popup?.hidePopover();
          document.removeEventListener('pointerdown', outside, true);
          document.removeEventListener('keydown', escape, true);
          document.removeEventListener('scroll', scroll, true);
          removeEventListener('resize', place);
        };
      }, [open, refresh]);
      const save = async (envIds) => {
        setBusy(true);
        ++requestSeq.current;
        try {
          const reply = await call('/v1/lan/session-scope', { session_id: sessionId, env_ids: envIds });
          if (reply.ok !== true) throw new Error(reply.error || '保存失败');
          if (live.current) { setData(reply); setError(''); }
          scopeEvents.dispatchEvent(new CustomEvent('changed', { detail: sessionId }));
        } catch (err) {
          if (live.current) setError(`未保存：${err.message || err}`);
        } finally { if (live.current) setBusy(false); }
      };
      const selected = data?.env_ids || [];
      const envs = data?.envs || [];
      const label = !data ? '浏览器环境' : selected.length === 0 ? '浏览器环境：未选择' :
        selected.length === 1 ? selected[0] : `浏览器环境：${selected.length} 个`;
      const buttonStyle = { display: 'inline-flex', alignItems: 'center', gap: 5, minHeight: 28,
        maxWidth: 200, padding: '0 8px', border: 0, borderRadius: 6, background: 'transparent',
        color: 'var(--dsw-alias-label-primary, #333)', font: 'inherit', fontSize: 12,
        cursor: 'pointer', whiteSpace: 'nowrap' };
      const shown = envs.filter(e => `${e.env_id} ${e.display_name || ''}`.toLowerCase().includes(query.toLowerCase()));
      return h('span', { 'data-lan-env-scope': sessionId, style: { display: 'inline-flex', minWidth: 0 } },
        h('button', { ref: anchor, type: 'button', 'aria-label': '浏览器环境',
          'aria-haspopup': 'dialog', 'aria-expanded': open,
          title: selected.length ? `本会话仅可操作：${selected.join('、')}` : '未选择环境时，不允许操作浏览器',
          style: buttonStyle, onClick: () => setOpen(x => !x) },
          h(LanGlyph, { size: 15 }), h('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis' } }, label),
          h('span', { 'aria-hidden': true }, '⌄')),
        open && h('div', { ref: menu, popover: 'manual', role: 'dialog', 'aria-label': '选择本会话的浏览器环境',
          'data-lan-env-menu': sessionId,
          style: { position: 'fixed', inset: 'auto', margin: 0, boxSizing: 'border-box',
            padding: 12, border: '1px solid var(--dsw-alias-border-l2, #ddd)', borderRadius: 10,
            // DSH's menu token is translucent (#f8f9fa94 in light mode).
            // This picker has no frosted backdrop: use an opaque theme surface.
            background: 'var(--dsw-alias-bg-base, white)', color: 'var(--dsw-alias-label-primary, #333)',
            boxShadow: '0 8px 30px #0002', fontSize: 13, overflow: 'auto' } },
          h('div', { style: { fontWeight: 600, marginBottom: 6 } }, '浏览器环境（可多选）'),
          h('div', { style: { fontSize: 12, opacity: .65, lineHeight: 1.6, marginBottom: 10 } },
            '仅在需要操作浏览器时使用所选环境。选择不会立即切换浏览器。'),
          h('input', { type: 'search', placeholder: '搜索环境名称或编号', 'aria-label': '搜索浏览器环境',
            value: query, onChange: e => setQuery(e.target.value),
            style: { boxSizing: 'border-box', width: '100%', padding: '7px 9px', marginBottom: 8,
              border: '1px solid var(--dsw-alias-border-l2, #ddd)', borderRadius: 6,
              background: 'transparent', color: 'inherit', font: 'inherit' } }),
          error && h('div', { role: 'alert', style: { color: '#c45532', padding: '6px 0' } }, error),
          !data ? h('div', null, error ? '环境不可用，请重试' : '正在读取环境…') :
            h('div', { role: 'group', 'aria-label': '允许操作的环境', style: { maxHeight: 230, overflowY: 'auto' } },
              ...shown.map(env => h('label', { key: env.env_id,
                style: { display: 'flex', alignItems: 'center', gap: 9, padding: '8px 4px', cursor: busy ? 'wait' : 'pointer' } },
                h('input', { type: 'checkbox', checked: selected.includes(env.env_id), disabled: busy,
                  'data-env-id': env.env_id, onChange: event => save(event.target.checked ?
                    [...selected, env.env_id] : selected.filter(id => id !== env.env_id)) }),
                h('span', null, env.env_id),
                env.display_name && env.display_name !== env.env_id &&
                  h('span', { style: { opacity: .65, overflow: 'hidden', textOverflow: 'ellipsis' } }, env.display_name))),
              shown.length === 0 && h('div', { style: { padding: 8, opacity: .65 } }, '没有匹配的环境')),
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 8, borderTop: '1px solid var(--dsw-alias-border-l2, #ddd)', paddingTop: 8, marginTop: 8 } },
            h('span', { role: 'status', style: { flex: 1, fontSize: 12, opacity: .65 } }, busy ? '保存中…' : `已选 ${selected.length} 个`),
            h('button', { type: 'button', style: buttonStyle, disabled: busy || !selected.length, onClick: () => save([]) }, '清空'),
            h('button', { type: 'button', style: buttonStyle, onClick: () => { setOpen(false); anchor.current?.focus(); } }, '完成'))));
    }

    function installEnvScopePicker(ctx) {
      ctx.effect(() => {
        const channel = new BroadcastChannel('lan-session-browser-scope');
        // Broadcast only local saves, not received notifications.
        const local = event => { if (!event.fromChannel) channel.postMessage(event.detail); };
        // CustomEvent does not preserve arbitrary initializer properties.
        channel.onmessage = event => {
          const notification = new CustomEvent('changed', { detail: event.data });
          notification.fromChannel = true;
          scopeEvents.dispatchEvent(notification);
        };
        scopeEvents.addEventListener('changed', local);
        return () => { scopeEvents.removeEventListener('changed', local); channel.close(); };
      }, 'lan-seats: sync browser selections across workbench windows');
      for (const [name, order] of [['conversation.hero.options', -100], ['conversation.session.header.actions', -9]]) {
        ctx.slots.inject(name, () => ctx.slots.register({ name, id: 'lan-browser-environments', order },
          props => h(EnvScopePicker, { ...props, key: props.sessionId })));
      }
    }

    function installSidebarEnvSwitcher(ctx) {
      ctx.effect(() => {
        const style = document.createElement('style');
        style.id = 'lan-env-sidebar-style';
        style.textContent = `
          .lan-env-sidebar-toggle{margin:0 5px;flex:none;display:grid;place-items:center;width:24px;height:24px;border:1px solid transparent;border-radius:6px;background:transparent;color:var(--dsw-alias-text-secondary,#687386);padding:0;cursor:pointer}
          .lan-env-sidebar-toggle:hover{border-color:var(--dsw-alias-border-l2,#d9dde5)}
          .lan-env-sidebar-toggle svg{display:block;width:16px;height:16px}
          .GAkraW_root[data-lan-env-mode=true] .GAkraW_sectionHeader{justify-content:flex-start}
          .GAkraW_root[data-lan-env-mode=true] .GAkraW_searchSlot,
          .GAkraW_root[data-lan-env-mode=true] .GAkraW_headerActions{display:none}
          .lan-env-sidebar-create{margin-left:auto;margin-right:4px;display:none;align-items:center}
          .GAkraW_root[data-lan-env-mode=true] .lan-env-sidebar-create{display:flex}
          .lan-env-sidebar-view{display:flex;flex-direction:column;gap:3px;min-height:0;overflow:auto;padding:2px 8px 12px}
          .lan-env-sidebar-view[hidden]{display:none}
          .lan-env-sidebar-empty{padding:12px 8px;color:var(--dsw-alias-text-secondary,#687386);font-size:12px}
          .lan-env-sidebar-env{display:block;width:100%;border:0;border-radius:6px;background:transparent;color:inherit;text-align:left;font:inherit;padding:7px 8px;cursor:pointer}
          .lan-env-sidebar-env:hover,.lan-env-sidebar-account:hover{background:rgba(90,120,170,.08)}
          .lan-env-sidebar-env[data-selected=true]{color:var(--dsw-alias-text-accent,#2367df);background:rgba(35,103,223,.07)}
          .lan-env-sidebar-name{display:grid;grid-template-columns:18px minmax(0,1fr) 28px 7px;align-items:center;gap:6px;min-width:0;font-size:13px;font-weight:600}
          .lan-env-sidebar-name>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
          .lan-env-sidebar-meta{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:2px 0 0 20px;color:var(--dsw-alias-text-secondary,#687386);font-size:11px}
          .lan-env-sidebar-accounts{display:grid;margin:0 0 3px 18px;padding-left:9px;border-left:1px solid var(--dsw-alias-border-l2,#d9dde5)}
          .lan-env-sidebar-account{display:grid;grid-template-columns:18px minmax(0,1fr) 28px 7px;align-items:center;gap:6px;min-width:0;width:100%;border:0;border-radius:5px;background:transparent;color:inherit;text-align:left;font:inherit;font-size:12px;padding:6px 8px;cursor:pointer}
          .lan-env-sidebar-account span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
          .lan-env-sidebar-dot{display:inline-block;width:9px;height:9px;flex:none;border-radius:50%;background:#7d8796;border:1px solid rgba(255,255,255,.92);box-shadow:0 0 0 1px rgba(71,82,99,.34),0 1px 4px rgba(30,41,59,.25);transition:background .18s ease,box-shadow .18s ease,transform .18s ease}
          .lan-env-sidebar-dot[data-state=unknown],.lan-env-sidebar-dot[data-state=stopped]{background:#8a94a3;box-shadow:0 0 0 1px rgba(71,82,99,.3),0 1px 3px rgba(30,41,59,.2)}
          .lan-env-sidebar-dot[data-state=running]{background:#16b364;box-shadow:0 0 0 1px rgba(4,120,87,.45),0 0 8px rgba(22,179,100,.72),0 1px 4px rgba(4,120,87,.35)}
          .lan-env-sidebar-dot[data-state=network-error]{background:#f59e0b;box-shadow:0 0 0 1px rgba(180,83,9,.52),0 0 9px rgba(245,158,11,.8),0 1px 4px rgba(180,83,9,.35);animation:lan-network-pulse 1.8s ease-in-out infinite}
          .lan-env-sidebar-dot[data-state=agent-running]{background:#2388ff;box-shadow:0 0 0 1px rgba(29,78,216,.58),0 0 10px rgba(35,136,255,.95),0 1px 5px rgba(29,78,216,.45);animation:lan-agent-pulse 1.15s ease-in-out infinite}
          .lan-env-sidebar-dot[data-state=agent-complete]{background:#4b72ff;box-shadow:0 0 0 1px rgba(49,46,129,.5),0 0 9px rgba(75,114,255,.88),0 1px 5px rgba(49,46,129,.38)}
          .lan-env-sidebar-dot[data-active=true]{transform:scale(1.08)}
          .lan-runtime-indicator[data-state=running]>span{color:#087443;font-weight:600}
          .lan-runtime-indicator[data-state=network-error]>span{color:#ad6100;font-weight:600}
          .lan-runtime-indicator[data-state=agent-running]>span,.lan-runtime-indicator[data-state=agent-complete]>span{color:#245dcc;font-weight:600}
          @keyframes lan-agent-pulse{0%,100%{opacity:1;box-shadow:0 0 0 1px rgba(29,78,216,.58),0 0 7px rgba(35,136,255,.72)}50%{opacity:.72;box-shadow:0 0 0 4px rgba(35,136,255,.14),0 0 15px rgba(35,136,255,.98)}}
          @keyframes lan-network-pulse{0%,100%{box-shadow:0 0 0 1px rgba(180,83,9,.52),0 0 6px rgba(245,158,11,.62)}50%{box-shadow:0 0 0 3px rgba(245,158,11,.14),0 0 12px rgba(245,158,11,.92)}}
          @media(prefers-reduced-motion:reduce){.lan-env-sidebar-dot{transition:none}.lan-env-sidebar-dot[data-state=agent-running],.lan-env-sidebar-dot[data-state=network-error]{animation:none}}
          .lan-current-badge{flex:none;width:28px;font-size:10px;font-weight:500;color:var(--dsw-alias-text-accent,#2367df);visibility:hidden}
          .lan-current-badge[data-current=true]{visibility:visible}
          .lan-env-sidebar-name>span:first-of-type,.lan-env-sidebar-account>span:first-of-type{flex:1;min-width:0}
          .lan-env-sidebar-meta[data-warning=true],.lan-runtime-indicator [data-warning=true]{color:#916000}
          .lan-runtime-indicator{display:inline-flex;align-items:center;gap:6px;min-width:0;overflow:hidden}
          .lan-runtime-indicator>span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
          .lan-env-sidebar-env:focus-visible,.lan-env-sidebar-account:focus-visible{outline:2px solid #276de1;outline-offset:-2px}
          .lan-env-sidebar-view[data-busy=true]{opacity:.82}
          .lan-env-sidebar-view [data-pending=true]{opacity:.68;cursor:progress}
        `;
        (document.head || document.documentElement).append(style);
        let mode = false, rows = [], active = '', confirmedActive = '', pendingAccount = '', observerQueued = 0, busy = false, queuedAction = null;
        let disposed=false, refreshing=null, refreshVersion=0, listSignature='', inFlightKey='', notice='';
        let runtime={states:{}},unsubscribeStatus=null,confirmedSession='';
        const syncStatus=()=>{if(mode&&!document.hidden&&!unsubscribeStatus)unsubscribeStatus=runtimeStatus.subscribe(data=>{runtime=data;if(!busy&&data.live_status==='ready'){active=confirmedActive=data.active||'';confirmedSession=data.active_session||'';if(active&&!rows.some(env=>env.env_id===active))refresh();}paintState();});else if((!mode||document.hidden)&&unsubscribeStatus){unsubscribeStatus();unsubscribeStatus=null;}};
        const controllers=new Set();
        let boundHeader = null, boundList = null, custom = null;
        const request = async (path, body={}, timeout=12000) => {
          const controller=new AbortController();controllers.add(controller);
          const timer=setTimeout(()=>controller.abort(),timeout);
          try {
            const response = await fetch(`${BASE}${path}`, {method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${TOKEN}`},body:JSON.stringify(body),signal:controller.signal});
            const data = await response.json();
            if (!response.ok || !data.ok) throw new Error(data.error || '操作失败');
            return data;
          } finally { clearTimeout(timer);controllers.delete(controller); }
        };
        const applyCatalog = data => {
          const nextRows=(data.envs||[]).map(env=>({env_id:env.env_id,display_name:env.display_name,account_count:env.account_count,selected_account_session:env.selected_account_session,
            accounts:(env.accounts||[]).map(a=>({account_id:a.account_id,session_env_id:a.session_env_id,platform_id:a.platform_id,platform:a.platform,url:a.url,username:a.username,login:a.login}))}));
          const signature=JSON.stringify(nextRows);
          if(signature!==listSignature){rows=nextRows;listSignature=signature;render();}else paintState();
        };
        const refresh = () => {
          if(disposed||busy)return;
          if(refreshing)return refreshing;
          const version=refreshVersion;
          refreshing=(async()=>{
            try {
              const data=await request('/v1/lan/manage/list');
              if(disposed||version!==refreshVersion)return;
              active=confirmedActive=data.active||'';notice='';
              applyCatalog(data);
            } catch { if(!disposed&&version===refreshVersion){notice='环境列表暂不可用，请切回后重试';paintState();} }
            finally {refreshing=null;}
          })();
          return refreshing;
        };
        const actionKey=(env,account)=>JSON.stringify([env.env_id,account?.account_id||'']);
        const activate = async (env, account, control) => {
          if(disposed)return;
          const key=actionKey(env,account);
          if (busy) { queuedAction=key===inFlightKey?null:{env,account};active=env.env_id;pendingAccount=account?.account_id||'';paintState();return; }
          busy=true;inFlightKey=key;refreshVersion++;notice='';runtimeStatus.pause();
          active=env.env_id;pendingAccount=account?.account_id||'';runtimeStatus.setActive(env.env_id,account?.session_env_id||env.selected_account_session||env.env_id);paintState();
          try {
            const result=account
              ? await request('/v1/lan/manage/accounts',{env_id:env.env_id,account_id:account.account_id,action:'open'},60000)
              : await request('/v1/lan/manage/open',{env_id:env.env_id},30000);
            if(disposed)return;
            confirmedActive=env.env_id;
            confirmedSession=result.session_env_id||account?.session_env_id||env.selected_account_session||env.env_id;
            if(account)env.selected_account_session=result.session_env_id||account.session_env_id;
          } catch(e) { if(!disposed)notice=e.name==='AbortError'?'切换等待超时，请确认浏览器状态后重试':e.message||'切换失败'; }
          finally {
            busy=false;inFlightKey='';
            const next=queuedAction;queuedAction=null;
            if(!disposed){if(next){await activate(next.env,next.account);}else{active=confirmedActive;pendingAccount='';runtimeStatus.setActive(confirmedActive,confirmedSession);paintState();}}
            runtimeStatus.resume();
          }
        };
        const paintState = () => {
          if(!custom) return;
          custom.dataset.busy=String(busy);custom.setAttribute('aria-busy',String(busy));
          let message=custom.querySelector('.lan-env-sidebar-notice');
          if(notice&&!message){message=document.createElement('div');message.className='lan-env-sidebar-empty lan-env-sidebar-notice';message.setAttribute('role','alert');custom.prepend(message);}
          if(message){if(notice)message.textContent=notice;else message.remove();}
          custom.querySelectorAll('.lan-env-sidebar-env').forEach(button=>{
            const selected=button.dataset.envId===active;
            button.dataset.selected=String(selected);
            button.setAttribute('aria-current',String(button.dataset.envId===confirmedActive));
            button.dataset.pending=String(busy&&selected);
            const dot=button.querySelector('.lan-env-sidebar-dot');if(dot){dot.dataset.active=String(selected);dot.dataset.state=(runtime.states[button.dataset.envId]||UNKNOWN_STATUS).state;}
            const current=button.dataset.envId===confirmedActive;const badge=button.querySelector('.lan-current-badge');if(badge)badge.dataset.current=String(current);
            const env=rows.find(e=>e.env_id===button.dataset.envId),s=runtime.states[button.dataset.envId]||UNKNOWN_STATUS,copy=statusCopy(s);
            const important=s.agent_active_count>1||s.network_warning||s.agent_warning;
            const meta=button.querySelector('.lan-env-sidebar-meta');if(meta){const text=(important?'':(env?.account_count||0)+' 个账号 · ')+copy.text;if(meta.textContent!==text)meta.textContent=text;meta.dataset.warning=String(!!s.network_warning||!!s.agent_warning);}
            button.title=(env?.display_name||button.dataset.envId)+' · '+(env?.account_count||0)+' 个账号 · '+copy.detail+(current?' · 当前':'');
          });
          const sessions=new Map(rows.map(env=>[env.env_id,env.selected_account_session]));
          custom.querySelectorAll('.lan-env-sidebar-account').forEach(button=>{
            const selected=button.dataset.envId===active;
            button.dataset.pending=String(busy&&selected&&button.dataset.accountId===pendingAccount);
            const current=button.dataset.envId===confirmedActive&&button.dataset.sessionId===confirmedSession;
            const s=runtime.states[button.dataset.sessionId]||UNKNOWN_STATUS,copy=statusCopy(s);
            const dot=button.querySelector('.lan-env-sidebar-dot');if(dot){dot.dataset.active=String(selected&&button.dataset.sessionId===sessions.get(button.dataset.envId));dot.dataset.state=s.state;}
            const badge=button.querySelector('.lan-current-badge');if(badge)badge.dataset.current=String(current);
            button.setAttribute('aria-current',String(current));button.setAttribute('aria-label',button.dataset.name+' · '+copy.detail+(current?' · 当前':''));button.title=button.dataset.name+' · '+copy.detail;
          });
        };
        const render = () => {
          if(!custom) return;
          custom.replaceChildren();
          if(!rows.length){const empty=document.createElement('div');empty.className='lan-env-sidebar-empty';empty.textContent=listSignature?'暂无环境':'正在读取环境…';custom.append(empty);paintState();return;}
          for(const env of rows){
            const item=document.createElement('div');item.className='lan-env-sidebar-item';item.dataset.envId=env.env_id;item.dataset.selectedSession=env.selected_account_session||'';
            const button=document.createElement('button');button.type='button';button.className='lan-env-sidebar-env';button.dataset.envId=env.env_id;button.setAttribute('aria-current',String(env.env_id===active));button.title=env.display_name+' · '+env.env_id;button.dataset.pending=String(busy&&env.env_id===active);
            const title=document.createElement('span');title.className='lan-env-sidebar-name';appendIcon(title,'environment');const name=document.createElement('span');name.textContent=env.display_name||env.env_id;const dot=document.createElement('i');dot.className='lan-env-sidebar-dot';dot.dataset.active=String(env.env_id===active);dot.setAttribute('aria-hidden','true');const badge=document.createElement('span');badge.className='lan-current-badge';badge.textContent='当前';title.append(name,badge,dot);
            const meta=document.createElement('small');meta.className='lan-env-sidebar-meta';meta.textContent=(env.env_id===env.display_name?'':env.env_id+' · ')+(env.account_count||0)+' 个账号';button.append(title,meta);button.addEventListener('click',()=>activate(env,null,button));item.append(button);
            if(env.accounts?.length){const accounts=document.createElement('div');accounts.className='lan-env-sidebar-accounts';for(const account of env.accounts){const row=document.createElement('button');row.type='button';row.className='lan-env-sidebar-account';row.dataset.envId=env.env_id;row.dataset.accountId=account.account_id;row.dataset.sessionId=account.session_env_id||'';row.title=[account.platform,account.username||account.login].filter(Boolean).join(' · ');row.dataset.pending=String(busy&&env.env_id===active&&account.account_id===pendingAccount);appendIcon(row,accountIconKey(account));const mark=document.createElement('i');mark.className='lan-env-sidebar-dot';mark.dataset.active=String(env.env_id===active&&account.session_env_id===env.selected_account_session);mark.setAttribute('aria-hidden','true');const label=document.createElement('span');label.textContent=[account.platform,account.username||account.login].filter(Boolean).join(' · ')||account.account_id;row.dataset.name=label.textContent;const badge=document.createElement('span');badge.className='lan-current-badge';badge.textContent='当前';row.append(label,badge,mark);row.addEventListener('click',()=>activate(env,account,row));accounts.append(row);}item.append(accounts);}
            custom.append(item);
          }
          paintState();
        };
        const setMode = value => {
          const nextMode=!!value,changed=nextMode!==mode;mode=nextMode;
          const header=boundHeader,list=boundList;
          if(header){const label=header.querySelector('.GAkraW_sectionLabel'),text=mode?'我的环境':'工作区';if(label&&label.textContent!==text)label.textContent=text;const root=header.closest('.GAkraW_root');root?.setAttribute('data-lan-env-mode',String(mode));const toggle=header.querySelector('.lan-env-sidebar-toggle');if(toggle){const label=mode?'切回工作区会话':'切换到我的环境';toggle.setAttribute('aria-label',label);toggle.title=label;toggle.setAttribute('aria-pressed',String(mode));}}
          if(list)list.style.display=mode?'none':'';
          if(mode){const root=boundHeader?.closest('.GAkraW_root');if(root&&!custom){custom=document.createElement('div');custom.className='lan-env-sidebar-view';custom.setAttribute('aria-label','环境和账号');}if(root&&custom&&custom.parentElement!==root)root.insertBefore(custom,boundList||null);else if(root&&custom&&!custom.isConnected)root.insertBefore(custom,boundList||null);if(custom){custom.hidden=false;if(changed)render();}if(changed)refresh();}
          else if(custom)custom.hidden=true;
          syncStatus();
          try{sessionStorage.setItem('lan-env-sidebar-mode',mode?'envs':'sessions')}catch{}
        };
        const bind = () => {
          const root=document.querySelector('.GAkraW_root'),header=root?.querySelector('.GAkraW_sectionHeader'),list=root?.querySelector('.GAkraW_listArea');
          if(!header||!list)return;
          if(header!==boundHeader){boundHeader=header;const toggle=document.createElement('button');toggle.type='button';toggle.className='lan-env-sidebar-toggle';toggle.setAttribute('aria-label','切换到我的环境');toggle.title='切换到我的环境';const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('fill','none');svg.setAttribute('aria-hidden','true');const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d','M7 15H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v2M11 9h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2Z');path.setAttribute('stroke','currentColor');path.setAttribute('stroke-width','1.7');path.setAttribute('stroke-linecap','round');path.setAttribute('stroke-linejoin','round');svg.append(path);toggle.append(svg);toggle.addEventListener('click',()=>setMode(!mode));const label=header.querySelector('.GAkraW_sectionLabel');if(label)label.after(toggle);else header.append(toggle);}
          if(!header.querySelector('.lan-env-sidebar-create')){
            const original=header.querySelector('button[aria-label="添加工作区"]');
            if(original){const wrap=document.createElement('div');wrap.className='lan-env-sidebar-create';const create=original.cloneNode(true);create.setAttribute('aria-label','新建环境');create.title='新建环境';create.removeAttribute('aria-expanded');create.addEventListener('click',event=>{event.stopPropagation();if(openEnvironmentCreator())console.info('LAN_OVERLAY_UI:'+JSON.stringify({action:'expand',sidebar_width:Math.round(document.querySelector('.mP3x-q_sidebarCol')?.getBoundingClientRect().width||280)}));});wrap.append(create);header.append(wrap);}
          }
          if(list!==boundList)boundList=list;
          const saved=(()=>{try{return sessionStorage.getItem('lan-env-sidebar-mode')==='envs'}catch{return false}})();
          setMode(mode||saved);
        };
        const observer=new MutationObserver(()=>{if(!observerQueued)observerQueued=requestAnimationFrame(()=>{observerQueued=0;bind();});});
        observer.observe(document.body,{childList:true,subtree:true});
        const onVisibility=()=>{syncStatus();if(document.visibilityState==='visible'&&mode&&!busy)refresh();};
        document.addEventListener('visibilitychange',onVisibility);
        const onImported=()=>{if(mode)refresh();};window.addEventListener('lan-env-imported',onImported);
        const onCatalogUpdated=event=>{if(disposed)return;refreshVersion++;notice='';applyCatalog(event.detail);};
        environmentCatalogEvents.addEventListener('updated',onCatalogUpdated);
        bind();
        return()=>{disposed=true;unsubscribeStatus?.();queuedAction=null;controllers.forEach(controller=>controller.abort());observer.disconnect();document.removeEventListener('visibilitychange',onVisibility);window.removeEventListener('lan-env-imported',onImported);environmentCatalogEvents.removeEventListener('updated',onCatalogUpdated);boundHeader?.querySelector('.lan-env-sidebar-create')?.remove();if(observerQueued)cancelAnimationFrame(observerQueued);style.remove();custom?.remove();boundHeader?.querySelector('.lan-env-sidebar-toggle')?.remove();const label=boundHeader?.querySelector('.GAkraW_sectionLabel');if(label&&label.textContent==='我的环境')label.textContent='工作区';boundHeader?.closest('.GAkraW_root')?.removeAttribute('data-lan-env-mode');if(boundList)boundList.style.display='';};
      }, 'lan-seats: environment and account sidebar switcher');
    }


    // The renderer remains wide; the browser clips only its visible native
    // surface and input area. All three UI states stay in the same WebContents.
    function installOverlayHost(ctx) {
      ctx.effect(() => {
        document.getElementById('lan-sidebar-only')?.remove();
        document.getElementById('lan-conversation-hidden')?.remove();
        document.getElementById('lan-conversation-shadow')?.remove();
        document.documentElement.removeAttribute('data-lan-collapsed');

        const shadowStyle = document.createElement('style');
        shadowStyle.id = 'lan-conversation-shadow';
        shadowStyle.textContent = `
          .mP3x-q_centerCol {
            position: relative !important;
            z-index: 1 !important;
            border-right: 1px solid rgba(15, 23, 42, 0.08) !important;
            box-shadow: 9px 0 26px -5px rgba(15, 23, 42, 0.20),
                        0 8px 24px -8px rgba(15, 23, 42, 0.16) !important;
          }
        `;
        (document.head || document.documentElement).append(shadowStyle);

        let lastState = '';
        let observed = null;
        let queued = 0;
        const sidebar = () => document.querySelector('.mP3x-q_sidebarCol');
        const send = (action) => {
          const width = Math.round(sidebar()?.getBoundingClientRect().width || 280);
          const visible = element => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
          const modal = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog[open]')].some(visible);
          const shell = !!(sidebar() && document.querySelector('.GAkraW_root'));
          const state = JSON.stringify({action, sidebar_width: width, shell_ready: shell, modal_visible: modal});
          if (action === 'ready' && state === lastState) return;
          lastState = state;
          console.info('LAN_OVERLAY_UI:' + state);
        };
        const resize = new ResizeObserver(() => send('ready'));
        const bind = () => {
          const current = sidebar();
          if (current && current !== observed) {
            resize.disconnect();
            observed = current;
            resize.observe(current);
            send('ready');
          }
        };
        const mountObserver = new MutationObserver(() => {
          if (!queued) queued = requestAnimationFrame(() => { queued = 0; bind(); send('ready'); });
        });
        mountObserver.observe(document.body, {childList: true, subtree: true, attributes: true,
          attributeFilter: ['class', 'style', 'open', 'aria-modal', 'role', 'hidden']});
        const onClick = (event) => {
          const target = event.target instanceof Element ? event.target : event.target?.parentElement;
          if (!target) return;
          // Selecting this main-slot panel changes DSH's route, but does not
          // reveal our native clip. Send intent on every click, including an
          // already-selected panel after click-away and the compact icon rail.
          // This expands the host only; DSH's conversation rightbar stays closed.
          if (target.closest('nav button[aria-label="我的环境"]')) {
            send('expand');
            return;
          }
          // Ignore the first click while the DSH shell is mounting.  Without a
          // rightbar opened yet, the shell's transient conversation controls can
          // otherwise emit an expand event before the environment panel mounts.
          if (!document.querySelector('.GAkraW_root')) return;
          // DSH owns every click inside its panel, including conversation padding.
          // Only the native host's click-away handler on the browsing page dismisses it.
          if (target.closest('.mP3x-q_sidebarCol')) {
            // Sidebar width and conversation visibility are independent.
            if (target.closest('button[aria-keyshortcuts="Control+Alt+B"]')) return;
            // Environment shortcuts switch browser sessions without opening the DSH conversation pane.
            // Switching environment/account is not opening the management panel.
            if (target.closest('.lan-env-sidebar-toggle, .lan-env-sidebar-env, .lan-env-sidebar-account, .lan-env-sidebar-create')) return;
            if (target.closest('[class*="sessionRow"], [class*="projectRow"], nav, button')) {
              send('expand');
            }
          }
        };
        document.addEventListener('click', onClick, true);
        bind();
        send('ready');
        return () => {
          document.removeEventListener('click', onClick, true);
          mountObserver.disconnect();
          resize.disconnect();
          if (queued) cancelAnimationFrame(queued);
          shadowStyle.remove();
        };
      }, 'lan-seats: native overlay viewport');
    }

    function apply(ctx) {
      installProductInfo(ctx);
      installOverlayHost(ctx);
      installEnvScopePicker(ctx);
      installSidebarEnvSwitcher(ctx);

      if (ctx.slots && typeof ctx.slots.inject === 'function') {
        ctx.slots.inject('main', function* () {
          yield ctx.slots.register({ name: 'main', key: 'lan-envs' }, LanEnvPanel);
        });
        ctx.slots.inject('sidebar.panellist', function* () {
          yield ctx.slots.register(
            { name: 'sidebar.panellist', id: 'lan-envs', order: 5, label: '我的环境' },
            LanGlyph,
          );
        });
        return;
      }
      ctx.slots.register({ name: 'main', key: 'lan-envs' }, LanEnvPanel);
      ctx.slots.register({ name: 'sidebar.panellist', id: 'lan-envs', order: 5, label: '我的环境' },
                         LanGlyph);
    }

    exports.inject = ['slots'];
    exports.apply = apply;
    return module.exports;
  },
});
