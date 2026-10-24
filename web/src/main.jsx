import "@ant-design/v5-patch-for-react-19";
import React, { useEffect, useState, useRef } from "react";
import { createRoot } from "react-dom/client";
import {
  App,
  ConfigProvider,
  Button,
  Input,
  Select,
  Table,
  Tag,
  Space,
  Modal,
  Drawer,
  Form,
  Switch,
  Steps,
  Collapse,
  Alert,
  Empty,
  Tabs,
  Checkbox,
  Spin,
  Tooltip,
  Popover,
  Progress,
  Grid,
} from "antd";
import zhCN from "antd/locale/zh_CN";
import {
  AppstoreOutlined,
  RobotOutlined,
  FolderOpenOutlined,
  GlobalOutlined,
  BellOutlined,
  ApiOutlined,
  HistoryOutlined,
  BranchesOutlined,
  MessageOutlined,
  PlusOutlined,
  ReloadOutlined,
  ArrowRightOutlined,
  LogoutOutlined,
  CheckCircleOutlined,
  SendOutlined,
  EditOutlined,
  DeleteOutlined,
  SettingOutlined,
  TagsOutlined,
  ShopOutlined,
} from "@ant-design/icons";
import "./style.css";
import useOperations from "./useOperations";
import WorkspacePanel from "./WorkspacePanel";
import ProxyPanel from "./ProxyPanel";
import TaxonomyPanel from "./TaxonomyPanel";
import SubscriptionPlaza from "./SubscriptionPlaza";
import ConversationPanel from "./ConversationPanel";
const api = async (path, method = "GET", body) => {
  const r = await fetch("/api/" + path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "workbench",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const v = await r.json();
  if (!r.ok) {
    if (r.status === 401 && !path.startsWith("auth/")) {
      window.dispatchEvent(new Event("workbench:session-expired"));
    }
    throw new Error(v.error || v.detail || "请求失败");
  }
  return v;
};
const STATES = {
  CONNECTED: ["已连接", "green"],
  READY: ["已就绪", "blue"],
  CONNECTING: ["连接中", "processing"],
  WAITING_QR: ["待扫码", "gold"],
  QR_EXPIRED: ["二维码已过期", "orange"],
  WAITING_TARGET: ["待接收目标", "orange"],
  RECONNECTING: ["重连中", "orange"],
  CONNECTION_FAILED: ["连接失败", "red"],
  QUEUED: ["待处理", "gold"],
  RUNNING: ["处理中", "processing"],
  RENDERED: ["已生成", "green"],
  WAITING_CONTENT: ["等待处理", "default"],
  SENDING: ["发送中", "processing"],
  ADAPTER_ACCEPTED: ["通道已接受", "green"],
  WAITING_CONTEXT: ["等待会话", "orange"],
  UNKNOWN: ["结果不确定", "red"],
  FAILED: ["失败", "red"],
  SKIPPED: ["已跳过", "default"],
  PAUSED: ["已暂停", "default"],
  CANCELLED: ["已取消", "default"],
};
const SOURCE_CHECK_RESULTS = {
  QUEUED: ["排队中", "default"],
  RUNNING: ["检查中", "processing"],
  OK: ["完成", "green"],
  BASELINE: ["已建基线", "green"],
  DEFERRED: ["间隔/冷却", "orange"],
  FAILED: ["失败", "red"],
  CANCELLED: ["已取消", "default"],
  INTERRUPTED: ["中断待核实", "red"],
};
const badge = (s) => <Tag color={STATES[s]?.[1]}>{STATES[s]?.[0] || s}</Tag>;
const subscriptionSourceIds = (row) =>
  row.source_ids || (row.source_id ? [row.source_id] : []);
const when = (v) =>
  v ? new Date(v * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
const NAV = [
  ["overview", AppstoreOutlined, "运行概览"],
  ["bots", RobotOutlined, "机器人"],
  ["workspaces", FolderOpenOutlined, "工作区"],
  ["sources", GlobalOutlined, "订阅源"],
  ["plaza", ShopOutlined, "订阅广场"],
  ["network", SettingOutlined, "采集网络"],
  ["subscriptions", BellOutlined, "订阅"],
  ["models", ApiOutlined, "模型"],
  ["conversations", MessageOutlined, "对话"],
  ["history", HistoryOutlined, "运行与回溯"],
  ["platforms", BranchesOutlined, "平台能力"],
];
function Login({ status, onDone }) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <div className="login-shell">
      <div className="login-intro">
        <div className="brand-symbol">S</div>
        <span className="eyebrow">SUBSCRIPTION WORKBENCH</span>
        <h1>
          让信息，
          <br />
          按你的规则抵达。
        </h1>
        <p>
          连接来源、工作区与机器人。
          <br />
          从每一次更新，到每一条推送。
        </p>
        <div className="login-lines">
          <span>多平台接入</span>
          <span>本地工作区</span>
          <span>完整回溯</span>
        </div>
      </div>
      <div className="login-card">
        <span className="eyebrow">
          {status.setup_required ? "首次使用" : "欢迎回来"}
        </span>
        <h2>
          {status.setup_required ? "设置工作台密码" : "登录订阅推送工作台"}
        </h2>
        <p>
          {status.setup_required
            ? "密码用于保护平台凭据、规则和运行记录。"
            : "使用管理员密码进入本地工作台。"}
        </p>
        {error && <Alert type="error" message={error} showIcon />}
        <Form
          layout="vertical"
          onFinish={async (v) => {
            setBusy(true);
            try {
              await api(
                "auth/" + (status.setup_required ? "setup" : "login"),
                "POST",
                v,
              );
              onDone();
            } catch (e) {
              setError(e.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <Form.Item
            name="password"
            label="管理员密码"
            rules={[
              { required: true, message: "请输入密码" },
              {
                min: status.setup_required ? 10 : 1,
                message: "至少 10 个字符",
              },
            ]}
          >
            <Input.Password
              size="large"
              autoComplete={
                status.setup_required ? "new-password" : "current-password"
              }
            />
          </Form.Item>
          <Button
            block
            type="primary"
            size="large"
            htmlType="submit"
            loading={busy}
          >
            {status.setup_required ? "创建并进入" : "登录工作台"}{" "}
            <ArrowRightOutlined />
          </Button>
        </Form>
      </div>
    </div>
  );
}
function Workbench() {
  const { message, modal } = App.useApp();
  const screens = Grid.useBreakpoint();
  const [auth, setAuth] = useState(null),
    [data, setData] = useState(null),
    [tab, setTab] = useState("overview"),
    [error, setError] = useState(""),
    [dialog, setDialog] = useState(null),
    [botStep, setBotStep] = useState(0),
    [newBot, setNewBot] = useState(null),
    [detail, setDetail] = useState(null),
    [artifact, setArtifact] = useState(null),
    [history, setHistory] = useState({ rows: [], total: 0 }),
    [historyKind, setHistoryKind] = useState("deliveries"),
    [historyPage, setHistoryPage] = useState(1),
    [historyState, setHistoryState] = useState(""),
    [search, setSearch] = useState("");
  const [taxonomyOpen, setTaxonomyOpen] = useState(false);
  const [sourceCategoryFilter, setSourceCategoryFilter] = useState(null);
  const [sourceTagFilter, setSourceTagFilter] = useState(null);
  const [sourceStatusFilter, setSourceStatusFilter] = useState(null);
  const [sourceCheck, setSourceCheck] = useState(null);
  const [sourceCheckId, setSourceCheckId] = useState(null);
  const [sourceChecks, setSourceChecks] = useState([]);
  const [sourceCheckOpen, setSourceCheckOpen] = useState(false);
  const [form] = Form.useForm();
  const operations = useOperations();
  const busy = !!operations.active["form-save"];
  const [selectedRows, setSelectedRows] = useState({});
  const [refreshTick, setRefreshTick] = useState(0);
  const refreshing = useRef(false);
  const expired = useRef(false);
  const selectedType = Form.useWatch("type", form),
    workspaceMode = Form.useWatch("workspace_mode", form),
    typedWorkspacePath = Form.useWatch("workspace_path", form),
    processingMode = Form.useWatch("mode", form),
    formCategory = Form.useWatch("category_id", form),
    formTagIds = Form.useWatch("tag_ids", form),
    formWorkspaceId = Form.useWatch("workspace_id", form);
  const joiningShared =
    !typedWorkspacePath?.trim() &&
    data?.workspaces?.find((w) =>
      w.path
        .split(String.fromCharCode(92))
        .join("/")
        .endsWith("/workspaces/shared"),
    );
  const check = async () => {
    try {
      setAuth(await api("auth/status"));
    } catch (e) {
      setError(e.message);
    }
  };
  const refresh = (fresh = false) => {
    if (refreshing.current)
      return fresh
        ? refreshing.current.then(() => refresh())
        : refreshing.current;
    const promise = api("overview")
      .then((result) => {
        setData(result);
        setError("");
        setRefreshTick((value) => value + 1);
        return result;
      })
      .catch((e) => {
        setError(e.message);
        return null;
      })
      .finally(() => {
        if (refreshing.current === promise) refreshing.current = null;
      });
    refreshing.current = promise;
    return promise;
  };
  useEffect(() => {
    check();
  }, []);
  useEffect(() => {
    if (auth?.authenticated) expired.current = false;
  }, [auth?.authenticated]);
  useEffect(() => {
    const onExpired = () => {
      if (expired.current) return;
      expired.current = true;
      setAuth((previous) =>
        previous ? { ...previous, authenticated: false } : previous,
      );
      setData(null);
      message.warning("登录已失效，请重新登录");
    };
    window.addEventListener("workbench:session-expired", onExpired);
    return () =>
      window.removeEventListener("workbench:session-expired", onExpired);
  }, [message]);
  useEffect(() => {
    if (!auth?.authenticated) return;
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, [auth?.authenticated]);
  useEffect(() => {
    if (!auth?.authenticated || tab !== "sources") return;
    let alive = true;
    const load = async () => {
      if (document.visibilityState === "hidden") return;
      try {
        const { batches } = await api("source-checks?limit=10");
        if (!alive) return;
        setSourceChecks(batches);
        setSourceCheckId((current) => current || batches[0]?.id || null);
        const key = sourceCheckId || batches[0]?.id;
        if (key) {
          const detail = await api(`source-checks/${key}`);
          if (alive) setSourceCheck(detail);
        }
      } catch (e) {
        if (alive) setError("读取检查记录失败：" + e.message);
      }
    };
    void load();
    const timer = setInterval(load, 2500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [auth?.authenticated, tab, sourceCheckId]);
  useEffect(() => {
    if (
      !dialog ||
      dialog.kind !== "bot" ||
      !newBot ||
      newBot.status !== "WAITING_QR"
    )
      return;
    let active = true,
      timer;
    const poll = async () => {
      try {
        const b = await api("bots/" + newBot.id + "/qr", "POST", {});
        if (active) {
          setNewBot(b);
          refresh();
          if (b.status === "WAITING_QR") timer = setTimeout(poll, 2000);
        }
      } catch (e) {
        if (active) {
          setError(e.message);
          timer = setTimeout(poll, 6000);
        }
      }
    };
    timer = setTimeout(poll, 1200);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [dialog?.kind, !!dialog, newBot?.id, newBot?.status]);
  useEffect(() => {
    if (tab !== "history" || !auth?.authenticated) return;
    let alive = true;
    const load = () => {
      if (document.visibilityState === "hidden") return;
      api(
        `history?kind=${historyKind}&page=${historyPage}&state=${historyState}&q=${encodeURIComponent(search)}`,
      )
        .then((v) => {
          if (alive) setHistory(v);
        })
        .catch((e) => {
          if (alive) setError("读取运行记录失败：" + e.message);
        });
    };
    load();
    const timer = setInterval(load, 10000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [
    tab,
    historyKind,
    historyPage,
    historyState,
    search,
    data?.stats?.inputs,
    JSON.stringify(data?.stats?.deliveries),
    refreshTick,
  ]);
  const execute = async (fn, ok, options = {}) => {
    const result = await operations.run(fn, { ...options, success: ok });
    if (result !== null) void refresh(true);
    return result;
  };
  const open = (kind, row = null) => {
    setDialog({ kind, row });
    setNewBot(null);
    setBotStep(0);
    form.resetFields();
    form.setFieldsValue(
      row
        ? {
            ...row,
            domains_text: (row.domains || []).join(", "),
          }
        : {
            type: kind === "bot" ? "weixin" : "rss",
            workspace_mode: "independent",
            enabled: true,
            poll_minutes: 15,
            proxy_mode: "inherit",
            capture_mode: "full",
            update_on_change: true,
            allow_undated: false,
            mode: "agent",
            tag_ids: [],
            config: {},
          },
    );
  };
  const subscribeFromPackage = async (manifest) => {
    const current = await refresh(true);
    if (!current) return;
    const category =
      manifest.categories.length === 1
        ? current.categories.find(
            (item) => item.key === manifest.categories[0].key,
          )
        : null;
    setTab("subscriptions");
    open("subscription");
    if (category)
      form.setFieldsValue({ category_id: category.id, tag_ids: [] });
  };
  const artifactOpen = async (job, kind = "report") => {
    const v = await execute(() => api(`artifacts/${job}?kind=${kind}`));
    if (v) setArtifact({ ...v, job, kind });
  };
  if (!auth)
    return (
      <div className="loading">
        <Spin />
        <p>{error || "正在连接本地服务…"}</p>
      </div>
    );
  if (!auth.authenticated) return <Login status={auth} onDone={check} />;
  if (!data)
    return (
      <div className="loading">
        <Spin />
        <p>{error || "正在读取工作台…"}</p>
      </div>
    );
  const { bots, workspaces, sources, subscriptions, models, platforms, stats } =
    data;
  const categories = data.categories || [],
    taxonomyTags = data.tags || [];
  const sourceStatus = (source) => {
    if (!source.enabled) return "paused";
    if (source.type === "webhook") return "webhook";
    if (source.cooldown_until > Date.now() / 1000) return "cooldown";
    if (source.health?.error) return "failed";
    return source.health?.baseline ? "healthy" : "pending";
  };
  const checkableSources = sources.filter(
    (source) => source.enabled && source.type !== "webhook",
  );
  const visibleSources = sources.filter(
    (source) =>
      source.name.toLowerCase().includes(search.toLowerCase()) &&
      (!sourceCategoryFilter || source.category_id === sourceCategoryFilter) &&
      (!sourceTagFilter || (source.tag_ids || []).includes(sourceTagFilter)) &&
      (!sourceStatusFilter || sourceStatus(source) === sourceStatusFilter),
  );
  const sourceCheckBusy =
    sourceChecks.some((batch) => ["QUEUED", "RUNNING"].includes(batch.state)) ||
    ["QUEUED", "RUNNING"].includes(sourceCheck?.state) ||
    Object.keys(operations.active).some(
      (key) => key === "batch-check-sources" || key.startsWith("check-"),
    );
  const categoryName = (id) =>
    categories.find((c) => c.id === id)?.name || "未分类";
  const tagOptions = (categoryId) =>
    taxonomyTags
      .filter((t) => t.category_id === categoryId)
      .map((t) => ({ label: t.name, value: t.id }));
  const subscriptionTagOptions = (categoryId) =>
    taxonomyTags
      .filter((t) => t.category_id === categoryId)
      .map((t) => ({
        label: `${t.name} · ${sources.filter((s) => (s.tag_ids || []).includes(t.id)).length} 个来源`,
        value: t.id,
      }));
  const matchingSources = sources.filter(
    (s) =>
      s.category_id === formCategory &&
      (formTagIds || []).some((id) => (s.tag_ids || []).includes(id)),
  );
  const receivingBots = bots.filter(
    (b) =>
      b.workspace_id === formWorkspaceId &&
      platforms.find((p) => p.id === b.type)?.push_capable !== false,
  );
  const workspaceName = (id) =>
      workspaces.find((w) => w.id === id)?.name || "待绑定",
    modelName = (id) => models.find((m) => m.id === id)?.name || "默认模型",
    platformName = (id) => platforms.find((p) => p.id === id)?.name || id,
    botName = (id) => bots.find((b) => b.id === id)?.name || id;
  const modelOptions = models.map((m) => ({ label: m.name, value: m.id })),
    workspaceOptions = workspaces.map((w) => ({
      label: w.name + " · " + w.path,
      value: w.id,
    }));
  const selectedPlatform = platforms.find((p) => p.id === selectedType);
  const kindRows = (kind) =>
    ({
      bot: bots,
      source: sources,
      subscription: subscriptions,
      model: models,
      workspace: workspaces,
    })[kind];
  const chosen = (kind) =>
    kindRows(kind).filter((row) => (selectedRows[kind] || []).includes(row.id));
  const selection = (kind) => ({
    selectedRowKeys: selectedRows[kind] || [],
    onChange: (keys) =>
      setSelectedRows((value) => ({ ...value, [kind]: keys })),
    preserveSelectedRowKeys: true,
    selections: [
      Table.SELECTION_ALL,
      Table.SELECTION_INVERT,
      Table.SELECTION_NONE,
    ],
    getCheckboxProps: (row) => ({ "aria-label": `选择 ${row.name}` }),
  });
  const labels = {
    bot: "机器人",
    source: "订阅源",
    subscription: "订阅",
    workspace: "工作区",
    model: "模型",
  };
  const manageRows = async (kind, action, rows) => {
    if (!rows.length) return;
    const body = {
      kind,
      action,
      items: rows.map(({ id, revision }) => ({ id, revision })),
    };
    const apply = async () => {
      const result = await execute(
        () => api("manage/apply", "POST", body),
        `已${action === "delete" ? "删除" : action === "enable" ? "启用" : "暂停"} ${rows.length} 个${labels[kind]}`,
        {
          key: `manage-${kind}-${rows.map((r) => r.id).join(",")}`,
          label: `${action === "delete" ? "删除" : action === "enable" ? "启用" : "暂停"}${labels[kind]}`,
        },
      );
      if (result) {
        setSelectedRows((value) => ({ ...value, [kind]: [] }));
        if (
          kind === "bot" &&
          action === "delete" &&
          rows.some((r) => r.id === detail)
        )
          setDetail(null);
      }
      return result;
    };
    if (action !== "delete") return apply();
    const impact = await execute(
      () => api("manage/preview", "POST", body),
      undefined,
      {
        key: `preview-${kind}-${rows.map((r) => r.id).join(",")}`,
        label: "检查删除影响",
      },
    );
    if (!impact) return;
    modal.confirm({
      title: `删除 ${rows.length} 个${labels[kind]}？`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      content: (
        <div>
          <p>{impact.names.join("、")}</p>
          {impact.related_subscriptions > 0 && (
            <p>同时移除 {impact.related_subscriptions} 个关联订阅。</p>
          )}
          {impact.updated_subscriptions > 0 && (
            <p>
              另有 {impact.updated_subscriptions}{" "}
              个订阅仅移除所选来源，其余来源保留。
            </p>
          )}
          {impact.pending_deliveries > 0 && (
            <p>取消 {impact.pending_deliveries} 条尚未发送的投递。</p>
          )}
          {kind === "model" && (
            <p>
              关联设置将改为使用其他默认模型；没有可用模型时，需重新配置后才能整理。
            </p>
          )}
          <p>本地目录、已生成内容和历史记录保留。</p>
        </div>
      ),
      onOk: async () => {
        const result = await apply();
        if (!result) throw new Error("删除未完成，请查看操作记录");
      },
    });
  };
  const checkSources = async (rows, label, scope) => {
    const result = await execute(
      () =>
        api(
          "source-checks",
          "POST",
          scope === "all"
            ? { scope }
            : { scope, source_ids: rows.map((source) => source.id) },
        ),
      "检查任务已创建，离开页面后仍会继续",
      { key: "batch-check-sources", label },
    );
    if (result) {
      setSourceCheck(result);
      setSourceCheckId(result.id);
      setSourceChecks((current) => [result, ...current].slice(0, 10));
    } else {
      const history = await api("source-checks?limit=10").catch(() => null);
      if (history) setSourceChecks(history.batches);
    }
    return result;
  };
  const confirmSourceCheck = (rows, label, scope) => {
    const eligible = rows.filter(
      (source) => source.enabled && source.type !== "webhook",
    );
    if (!eligible.length || sourceCheckBusy) return;
    modal.confirm({
      title: `${label}？`,
      content: (
        <div>
          <p>
            将检查 {eligible.length} 个已启用的 RSS、网页或 API 来源；跳过{" "}
            {rows.length - eligible.length} 个暂停或 Webhook 来源。
          </p>
          <p>
            这是正常采集：遵守轮询间隔与站点冷却，不补发历史；发现新事件时可能触发整理和真实推送。
          </p>
        </div>
      ),
      okText: "开始检查",
      onOk: () => {
        void checkSources(eligible, label, scope);
      },
    });
  };
  const batchBar = (kind) =>
    chosen(kind).length > 0 && (
      <div className="batch-toolbar">
        <Space wrap>
          <b>
            已选 {chosen(kind).length} 个{labels[kind]}
          </b>
          {["bot", "source", "subscription"].includes(kind) && (
            <>
              <Button
                loading={Object.keys(operations.active).some(
                  (key) =>
                    key.startsWith(`manage-${kind}-`) ||
                    key.startsWith(`preview-${kind}-`),
                )}
                onClick={() => manageRows(kind, "enable", chosen(kind))}
              >
                批量启用
              </Button>
              <Button
                loading={Object.keys(operations.active).some(
                  (key) =>
                    key.startsWith(`manage-${kind}-`) ||
                    key.startsWith(`preview-${kind}-`),
                )}
                onClick={() => manageRows(kind, "disable", chosen(kind))}
              >
                批量暂停
              </Button>
            </>
          )}
          {kind === "source" && (
            <Button
              disabled={
                sourceCheckBusy ||
                chosen(kind).every(
                  (source) => source.type === "webhook" || !source.enabled,
                )
              }
              title={
                chosen(kind).every(
                  (source) => source.type === "webhook" || !source.enabled,
                )
                  ? "暂停的来源和 Webhook 不参与轮询检查"
                  : undefined
              }
              loading={!!operations.active["batch-check-sources"]}
              onClick={() =>
                confirmSourceCheck(chosen(kind), "检查所选订阅源", "selected")
              }
            >
              检查所选
            </Button>
          )}
          <Button
            danger
            icon={<DeleteOutlined />}
            aria-label={`批量删除${labels[kind]}`}
            loading={Object.keys(operations.active).some(
              (key) =>
                key.startsWith(`manage-${kind}-`) ||
                key.startsWith(`preview-${kind}-`),
            )}
            onClick={() => manageRows(kind, "delete", chosen(kind))}
          >
            批量删除
          </Button>
          <Button
            type="text"
            onClick={() =>
              setSelectedRows((value) => ({ ...value, [kind]: [] }))
            }
          >
            取消选择
          </Button>
        </Space>
      </div>
    );

  const save = async () => {
    let v;
    try {
      v = await form.validateFields();
    } catch {
      return;
    }
    const formExecute = (fn, ok) =>
      execute(fn, ok, {
        key: "form-save",
        label: {
          bot: "接入机器人",
          source: "保存订阅源",
          subscription: "保存订阅",
          model: "保存模型",
        }[dialog.kind],
      });
    let r;
    if (dialog.kind === "bot") {
      r = await formExecute(() =>
        api("bots", "POST", {
          name: v.name,
          type: v.type,
          config: v.config || {},
          workspace_mode:
            v.workspace_mode === "existing" ? "independent" : v.workspace_mode,
          workspace_id:
            v.workspace_mode === "existing" ? v.workspace_id : undefined,
          workspace_path: v.workspace_path,
          workspace_name:
            v.workspace_mode === "shared" && !joiningShared
              ? v.workspace_name
              : undefined,
        }),
      );
      if (r) {
        setNewBot(r);
        setBotStep(2);
      }
      return;
    }
    if (dialog.kind === "source") {
      let x = {
        ...dialog.row,
        ...v,
        domains: v.domains_text
          ? v.domains_text
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean)
          : v.url
            ? [new URL(v.url).hostname]
            : [],
      };
      delete x.health;
      r = await formExecute(() => api("sources", "POST", x), "来源已保存");
      if (r?.hook_token)
        modal.info({
          title: "保存 Webhook 接入信息",
          width: 620,
          content: (
            <div>
              <p>此凭据只展示一次，请保存到调用方。</p>
              <pre>{r.hook_path}</pre>
              <pre>Authorization: Bearer {r.hook_token}</pre>
              <p>事件正文包含 id、title 和 text。</p>
            </div>
          ),
        });
    }
    if (dialog.kind === "subscription")
      r = await formExecute(
        () =>
          api("subscriptions", "POST", {
            ...dialog.row,
            ...v,
            source_id: undefined,
            category_id: v.category_id,
            tag_ids: v.tag_ids,
            source_ids: [],
            model_id: v.mode === "agent" ? v.model_id || null : null,
          }),
        "订阅已保存",
      );
    if (dialog.kind === "model") {
      const x = { ...dialog.row, ...v };
      if (!x.api_key) delete x.api_key;
      r = await formExecute(() => api("models", "POST", x), "模型配置已保存");
    }
    if (r) setDialog(null);
  };
  const addActions = {
    bots: ["bot", "新增机器人"],
    sources: ["source", "新增订阅源"],
    subscriptions: ["subscription", "新增订阅"],
    models: ["model", "添加模型"],
  };
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-symbol">S</div>
          <div>
            订阅推送<small>WORKBENCH</small>
          </div>
        </div>
        <nav>
          {NAV.map(([id, Icon, label]) => (
            <button
              key={id}
              aria-label={label}
              className={tab === id ? "active" : ""}
              onClick={() => {
                setTab(id);
                setSearch("");
              }}
            >
              <Icon />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>
            <i /> 独立服务运行中
          </div>
          <Button
            type="text"
            icon={<LogoutOutlined />}
            onClick={async () => {
              await api("auth/logout", "POST", {});
              setData(null);
              check();
            }}
          >
            退出登录
          </Button>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span className="eyebrow">SUBSCRIPTION WORKBENCH</span>
            <h1>{NAV.find((n) => n[0] === tab)?.[2]}</h1>
          </div>
          <Space wrap>
            <Popover
              trigger="click"
              placement="bottomRight"
              content={
                <div className="operation-list">
                  <h3>最近操作</h3>
                  {operations.records.length ? (
                    operations.records.map((r) => (
                      <div className="operation-row" key={r.key}>
                        <div>
                          <b>{r.label}</b>
                          {r.state === "running" ? (
                            <Spin size="small" />
                          ) : (
                            <Tag
                              color={
                                r.state === "error"
                                  ? "red"
                                  : r.state === "warning"
                                    ? "orange"
                                    : "green"
                              }
                            >
                              {r.state === "error"
                                ? "失败"
                                : r.state === "warning"
                                  ? "已结束"
                                  : "完成"}
                            </Tag>
                          )}
                        </div>
                        <small>{r.detail}</small>
                        <small>
                          {new Date(r.started).toLocaleTimeString("zh-CN")}{" "}
                          {r.ended
                            ? `· ${((r.ended - r.started) / 1000).toFixed(1)} 秒`
                            : "· 正在执行"}
                        </small>
                      </div>
                    ))
                  ) : (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description="暂无手动操作"
                    />
                  )}
                </div>
              }
            >
              <Button icon={<HistoryOutlined />} aria-label="操作记录">
                操作记录
                {Object.keys(operations.active).length > 0
                  ? `（${Object.keys(operations.active).length}）`
                  : ""}
              </Button>
            </Popover>
            <Button
              aria-label="刷新"
              icon={<ReloadOutlined />}
              loading={!!operations.active.refresh}
              onClick={() =>
                execute(
                  async () => {
                    const result = await refresh(true);
                    if (!result) throw new Error("读取工作台状态失败，请重试");
                    return result;
                  },
                  "页面状态已更新",
                  { key: "refresh", label: "刷新工作台" },
                )
              }
            >
              刷新
            </Button>
            {tab === "sources" && (
              <Tooltip
                title={`已启用且可轮询的来源 ${checkableSources.length} 个；暂停及 Webhook 不参与`}
              >
                <Button
                  aria-label="检查全部"
                  icon={<CheckCircleOutlined />}
                  disabled={!checkableSources.length || sourceCheckBusy}
                  loading={!!operations.active["batch-check-sources"]}
                  onClick={() =>
                    confirmSourceCheck(sources, "检查全部订阅源", "all")
                  }
                >
                  检查全部
                </Button>
              </Tooltip>
            )}
            {addActions[tab] && (
              <Button
                aria-label={addActions[tab][1]}
                type="primary"
                icon={<PlusOutlined />}
                disabled={tab === "subscriptions" && !workspaces.length}
                onClick={() => open(addActions[tab][0])}
              >
                {addActions[tab][1]}
              </Button>
            )}
          </Space>
        </header>
        {error && (
          <Alert
            type="error"
            message={error}
            closable
            onClose={() => setError("")}
            style={{ marginBottom: 20 }}
          />
        )}
        {tab === "overview" && (
          <>
            <div className="stats">
              {[
                ["已接入机器人", bots.length, "真实平台实例"],
                ["本地工作区", workspaces.length, "共享或独立目录"],
                [
                  "已启用订阅",
                  subscriptions.filter((s) => s.enabled).length,
                  `${sources.length} 个来源配置`,
                ],
                [
                  "已接受投递",
                  stats.deliveries.ADAPTER_ACCEPTED || 0,
                  "各目标独立记录",
                ],
              ].map(([label, value, note]) => (
                <div className="stat" key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                  <small>{note}</small>
                </div>
              ))}
            </div>
            {!bots.length ? (
              <div className="start-panel">
                <div className="hero-icon">
                  <RobotOutlined />
                </div>
                <span className="eyebrow">从第一个机器人开始</span>
                <h2>接入平台，绑定你的本地工作区。</h2>
                <p>命名并选择类型，完成扫码或配置，工作区自动就绪。</p>
                <Button
                  aria-label="新增机器人"
                  type="primary"
                  size="large"
                  icon={<PlusOutlined />}
                  onClick={() => open("bot")}
                >
                  新增机器人
                </Button>
                <div className="start-steps">
                  <span>01 命名与类型</span>
                  <ArrowRightOutlined />
                  <span>02 平台配置</span>
                  <ArrowRightOutlined />
                  <span>03 自动绑定</span>
                </div>
              </div>
            ) : (
              <div className="two-col">
                <section className="panel">
                  <div className="panel-title">
                    <h2>机器人连接</h2>
                    <Button type="link" onClick={() => setTab("bots")}>
                      管理机器人
                    </Button>
                  </div>
                  {bots.map((b) => (
                    <div className="list-row" key={b.id}>
                      <RobotOutlined />
                      <div>
                        <b>{b.name}</b>
                        <small>
                          {platformName(b.type)} ·{" "}
                          {workspaceName(b.workspace_id)}
                        </small>
                      </div>
                      {badge(b.enabled ? b.status : "PAUSED")}
                    </div>
                  ))}
                </section>
                <section className="panel">
                  <div className="panel-title">
                    <h2>处理与投递</h2>
                  </div>
                  <div className="counter-row">
                    <span>已采集事件</span>
                    <b>{stats.inputs}</b>
                  </div>
                  {Object.entries(stats.jobs).map(([s, n]) => (
                    <div className="counter-row" key={s}>
                      {badge(s)}
                      <b>{n}</b>
                    </div>
                  ))}
                  {!Object.keys(stats.jobs).length && (
                    <Empty
                      description="有新的订阅事件时，将在这里显示"
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                    />
                  )}
                </section>
              </div>
            )}
          </>
        )}

        {tab === "bots" && (
          <section className="panel">
            {batchBar("bot")}
            <Table
              rowKey="id"
              rowSelection={selection("bot")}
              dataSource={bots}
              pagination={false}
              locale={{
                emptyText: (
                  <Empty description="接入第一个机器人，即可自动建立工作区" />
                ),
              }}
              columns={[
                {
                  title: "机器人",
                  render: (_, b) => (
                    <div className="name-cell">
                      <RobotOutlined />
                      <div>
                        <b>{b.name}</b>
                        <small>{platformName(b.type)}</small>
                      </div>
                    </div>
                  ),
                },
                {
                  title: "连接状态",
                  render: (_, b) => (
                    <Tooltip title={b.error}>
                      {badge(b.enabled ? b.status : "PAUSED")}
                    </Tooltip>
                  ),
                },
                {
                  title: "本地工作区",
                  render: (_, b) => (
                    <div>
                      <b>{workspaceName(b.workspace_id)}</b>
                      <small className="path">
                        {b.workspace_path || "接入成功后自动绑定"}
                      </small>
                    </div>
                  ),
                },
                {
                  title: "接收目标",
                  render: (_, b) =>
                    b.targets
                      .filter((t) => t.selected)
                      .map((t) => t.name)
                      .join("、") || "等待会话",
                },
                {
                  title: "启用",
                  render: (_, b) => (
                    <Switch
                      checked={b.enabled}
                      onChange={(enabled) =>
                        manageRows("bot", enabled ? "enable" : "disable", [b])
                      }
                    />
                  ),
                },
                {
                  title: "操作",
                  render: (_, b) => (
                    <Space>
                      <Button
                        icon={<EditOutlined />}
                        onClick={() => setDetail(b.id)}
                      >
                        管理
                      </Button>
                      <Button
                        danger
                        aria-label={`删除机器人 ${b.name}`}
                        icon={<DeleteOutlined />}
                        onClick={() => manageRows("bot", "delete", [b])}
                      >
                        删除
                      </Button>
                    </Space>
                  ),
                },
              ]}
              scroll={{ x: 950 }}
            />
          </section>
        )}
        {tab === "workspaces" && (
          <WorkspacePanel
            data={data}
            api={api}
            execute={execute}
            active={operations.active}
            onEditSubscription={(row) => open("subscription", row)}
            onAddSubscription={(id) => {
              open("subscription");
              form.setFieldValue("workspace_id", id);
            }}
            onDelete={(kind, row) => manageRows(kind, "delete", [row])}
            onBatch={manageRows}
          />
        )}
        {tab === "network" && (
          <ProxyPanel
            data={data}
            api={api}
            execute={execute}
            active={operations.active}
          />
        )}
        {tab === "plaza" && (
          <SubscriptionPlaza
            data={data}
            api={api}
            execute={execute}
            active={operations.active}
            onSubscribe={subscribeFromPackage}
          />
        )}
        {tab === "sources" && (
          <section className="panel">
            {batchBar("source")}
            <div className="table-tools source-tools">
              <span>
                显示 {visibleSources.length} / {sources.length} 个来源 ·
                按订阅启用采集
              </span>
              <Select
                allowClear
                placeholder="全部分类"
                value={sourceCategoryFilter}
                style={{ minWidth: 145 }}
                options={categories.map((c) => ({
                  label: c.name,
                  value: c.id,
                }))}
                onChange={(id) => {
                  setSourceCategoryFilter(id);
                  setSourceTagFilter(null);
                }}
              />
              <Select
                allowClear
                placeholder="全部标签"
                value={sourceTagFilter}
                style={{ minWidth: 145 }}
                disabled={!sourceCategoryFilter}
                options={tagOptions(sourceCategoryFilter)}
                onChange={setSourceTagFilter}
              />
              <Select
                aria-label="来源状态"
                allowClear
                placeholder="全部状态"
                value={sourceStatusFilter}
                style={{ minWidth: 145 }}
                options={[
                  { label: "请求失败", value: "failed" },
                  { label: "冷却中", value: "cooldown" },
                  { label: "待建基线", value: "pending" },
                  { label: "已建立基线", value: "healthy" },
                  { label: "已暂停", value: "paused" },
                  { label: "Webhook 事件", value: "webhook" },
                ]}
                onChange={setSourceStatusFilter}
              />
              <Input.Search
                allowClear
                placeholder="搜索来源"
                onChange={(e) => setSearch(e.target.value)}
                style={{ width: 210 }}
              />
              <Button
                icon={<TagsOutlined />}
                onClick={() => setTaxonomyOpen(true)}
              >
                分类与标签
              </Button>
            </div>
            {sourceCheck && (
              <Alert
                className="source-check-progress"
                type={
                  ["QUEUED", "RUNNING"].includes(sourceCheck.state)
                    ? "info"
                    : sourceCheck.warning
                      ? "warning"
                      : "success"
                }
                showIcon
                message={
                  ["QUEUED", "RUNNING"].includes(sourceCheck.state)
                    ? `检查任务进行中 · ${sourceCheck.done}/${sourceCheck.total}${sourceCheck.stop_requested ? " · 正在停止" : ""}`
                    : sourceCheck.summary
                }
                description={
                  ["QUEUED", "RUNNING"].includes(sourceCheck.state) ? (
                    <Progress
                      percent={Math.round(
                        (sourceCheck.done / (sourceCheck.total || 1)) * 100,
                      )}
                      size="small"
                      status="active"
                    />
                  ) : undefined
                }
                action={
                  <Button size="small" onClick={() => setSourceCheckOpen(true)}>
                    查看明细
                  </Button>
                }
              />
            )}
            <Table
              rowKey="id"
              rowSelection={{ ...selection("source"), fixed: !!screens.lg }}
              dataSource={visibleSources}
              pagination={{ pageSize: 12, showSizeChanger: false }}
              columns={[
                {
                  title: "订阅范围",
                  width: 250,
                  fixed: screens.lg ? "left" : undefined,
                  render: (_, s) => (
                    <div>
                      <b>{s.name}</b>
                      <small>
                        {s.type === "webhook" ? "外部事件接入" : s.url}
                      </small>
                    </div>
                  ),
                },
                {
                  title: "类型",
                  dataIndex: "type",
                  width: 115,
                  render: (v) => (
                    <Tag>
                      {
                        {
                          rss: "RSS / Atom",
                          webpage: "网页变更",
                          api: "JSON API",
                          webhook: "Webhook 事件",
                        }[v]
                      }
                    </Tag>
                  ),
                },
                {
                  title: "分类 / 标签",
                  width: 190,
                  render: (_, s) => (
                    <div>
                      <span>{categoryName(s.category_id)}</span>
                      <div>
                        {(s.tag_ids || []).map((id) => (
                          <Tag key={id}>
                            {taxonomyTags.find((t) => t.id === id)?.name || id}
                          </Tag>
                        ))}
                      </div>
                    </div>
                  ),
                },
                {
                  title: "网络",
                  width: 100,
                  render: (_, s) => (
                    <Tag>
                      {s.proxy_mode === "direct"
                        ? "直连"
                        : s.proxy_mode === "proxy"
                          ? "代理"
                          : data.network?.enabled
                            ? "全局代理"
                            : "全局直连"}
                    </Tag>
                  ),
                },
                {
                  title: "间隔",
                  width: 92,
                  render: (_, s) =>
                    s.type === "webhook"
                      ? "事件触发"
                      : s.poll_minutes + " 分钟",
                },
                {
                  title: "采集状态",
                  width: 135,
                  render: (_, s) =>
                    !s.enabled ? (
                      <Tag>已暂停</Tag>
                    ) : s.type === "webhook" ? (
                      <Tag>事件触发</Tag>
                    ) : s.cooldown_until > Date.now() / 1000 ? (
                      <Tooltip title={when(s.cooldown_until)}>
                        <Tag color="orange">冷却中</Tag>
                      </Tooltip>
                    ) : s.health.error ? (
                      <Tooltip title={s.health.error}>
                        <Tag color="red">请求失败</Tag>
                      </Tooltip>
                    ) : s.health.baseline ? (
                      <Tag color="green">已建立基线</Tag>
                    ) : (
                      <Tag>待订阅 / 待建基线</Tag>
                    ),
                },
                {
                  title: "最近检查",
                  width: 170,
                  render: (_, s) => when(s.health.last_check),
                },
                {
                  title: "操作",
                  width: 210,
                  fixed: screens.lg ? "right" : undefined,
                  render: (_, s) => (
                    <Space>
                      <Button type="text" onClick={() => open("source", s)}>
                        编辑
                      </Button>
                      <Button
                        danger
                        type="text"
                        aria-label={`删除订阅源 ${s.name}`}
                        onClick={() => manageRows("source", "delete", [s])}
                      >
                        删除
                      </Button>
                      {s.type !== "webhook" && (
                        <Tooltip
                          title={
                            s.enabled
                              ? "正常采集，发现新事件时可能触发推送"
                              : "先启用来源再检查"
                          }
                        >
                          <Button
                            type="text"
                            aria-label={`检查订阅源 ${s.name}`}
                            disabled={!s.enabled || sourceCheckBusy}
                            loading={!!operations.active[`check-${s.id}`]}
                            onClick={() =>
                              execute(
                                () => api(`sources/${s.id}/poll`, "POST", {}),
                                undefined,
                                {
                                  key: `check-${s.id}`,
                                  label: `检查「${s.name}」`,
                                },
                              )
                            }
                          >
                            检查
                          </Button>
                        </Tooltip>
                      )}
                    </Space>
                  ),
                },
              ]}
              scroll={{ x: 1330 }}
            />
            <Drawer
              title="订阅源检查结果"
              width={720}
              open={sourceCheckOpen && !!sourceCheck}
              onClose={() => setSourceCheckOpen(false)}
            >
              <Space wrap style={{ marginBottom: 16 }}>
                <Select
                  aria-label="检查任务记录"
                  style={{ minWidth: 300, maxWidth: "100%" }}
                  value={sourceCheckId}
                  options={sourceChecks.map((batch) => ({
                    value: batch.id,
                    label: `${when(batch.created)} · ${batch.scope === "all" ? "全部" : "所选"} · ${batch.done}/${batch.total}`,
                  }))}
                  onChange={(key) => setSourceCheckId(key)}
                />
                {["QUEUED", "RUNNING"].includes(sourceCheck?.state) &&
                  !sourceCheck?.stop_requested && (
                    <Button
                      danger
                      loading={!!operations.active["stop-source-check"]}
                      onClick={async () => {
                        const result = await execute(
                          () =>
                            api(
                              `source-checks/${sourceCheck.id}/stop`,
                              "POST",
                              {},
                            ),
                          "已停止未开始的检查；已开始的检查会结束并记录结果",
                          { key: "stop-source-check", label: "停止后续检查" },
                        );
                        if (result) setSourceCheck(result);
                      }}
                    >
                      停止后续检查
                    </Button>
                  )}
              </Space>
              <p>
                {sourceCheck?.summary ||
                  `已检查 ${sourceCheck?.done || 0}/${sourceCheck?.total || 0} 个来源`}
              </p>
              <Table
                rowKey="id"
                dataSource={sourceCheck?.rows || []}
                pagination={{ pageSize: 20, showSizeChanger: false }}
                columns={[
                  { title: "订阅源", dataIndex: "name", width: 170 },
                  {
                    title: "结果",
                    dataIndex: "state",
                    width: 115,
                    render: (state) => (
                      <Tag color={SOURCE_CHECK_RESULTS[state]?.[1]}>
                        {SOURCE_CHECK_RESULTS[state]?.[0] || state}
                      </Tag>
                    ),
                  },
                  {
                    title: "读取",
                    dataIndex: "items",
                    width: 65,
                    render: (v) => v ?? "—",
                  },
                  {
                    title: "新事件",
                    dataIndex: "new_events",
                    width: 85,
                    render: (v) => v ?? "—",
                  },
                  {
                    title: "详情",
                    width: 260,
                    render: (_, row) =>
                      row.retry_at
                        ? `${row.detail}；下次可检查：${when(row.retry_at)}`
                        : row.detail || "—",
                  },
                ]}
                scroll={{ x: 695 }}
              />
            </Drawer>
          </section>
        )}
        {tab === "subscriptions" && (
          <section className="panel">
            {batchBar("subscription")}
            <Table
              rowKey="id"
              rowSelection={selection("subscription")}
              dataSource={subscriptions}
              columns={[
                { title: "订阅名称", dataIndex: "name" },
                {
                  title: "工作区",
                  dataIndex: "workspace_id",
                  render: workspaceName,
                },
                {
                  title: "来源",
                  render: (_, s) => {
                    const names = (
                      s.resolved_source_ids || subscriptionSourceIds(s)
                    ).map((id) => sources.find((x) => x.id === id)?.name || id);
                    return (
                      <Tooltip title={names.join("、")}>
                        <span>
                          {s.category_id
                            ? `${categoryName(s.category_id)}${s.tag_ids?.length ? " · " + s.tag_ids.map((id) => taxonomyTags.find((t) => t.id === id)?.name || id).join("、") : " · 全部标签"}：`
                            : "历史指定来源："}
                          {names.slice(0, 2).join("、")}
                          {names.length > 2 ? ` 等 ${names.length} 个` : ""}
                          {!names.length ? "暂无匹配来源" : ""}
                        </span>
                      </Tooltip>
                    );
                  },
                },
                {
                  title: "处理方式",
                  dataIndex: "mode",
                  render: (m) =>
                    ({
                      forward: "直接转发",
                      rules: "规则处理",
                      agent: "Agent 整理",
                    })[m],
                },
                {
                  title: "模型",
                  render: (_, row) =>
                    row.mode === "agent" ? (
                      modelName(row.model_id)
                    ) : (
                      <Tag>不调用模型</Tag>
                    ),
                },
                {
                  title: "启用",
                  render: (_, s) => (
                    <Switch
                      checked={s.enabled}
                      onChange={(enabled) =>
                        manageRows(
                          "subscription",
                          enabled ? "enable" : "disable",
                          [s],
                        )
                      }
                    />
                  ),
                },
                {
                  title: "操作",
                  render: (_, s) => (
                    <Space>
                      <Button
                        type="text"
                        onClick={() => open("subscription", s)}
                      >
                        编辑
                      </Button>
                      <Button
                        danger
                        type="text"
                        aria-label={`删除订阅 ${s.name}`}
                        onClick={() =>
                          manageRows("subscription", "delete", [s])
                        }
                      >
                        删除
                      </Button>
                    </Space>
                  ),
                },
              ]}
              scroll={{ x: 850 }}
            />
          </section>
        )}
        {tab === "models" && (
          <section className="panel">
            {batchBar("model")}
            <Table
              rowKey="id"
              rowSelection={selection("model")}
              dataSource={models}
              pagination={false}
              columns={[
                {
                  title: "模型配置",
                  render: (_, m) => (
                    <div>
                      <b>{m.name}</b>
                      {m.id === data.default_model && (
                        <Tag color="green" style={{ marginLeft: 8 }}>
                          默认
                        </Tag>
                      )}
                      <small>{m.model}</small>
                    </div>
                  ),
                },
                { title: "服务地址", dataIndex: "base_url" },
                {
                  title: "密钥",
                  render: (_, m) => (
                    <Tag>{m.has_key ? "已加密保存" : "未设置"}</Tag>
                  ),
                },
                {
                  title: "操作",
                  render: (_, m) => (
                    <Space>
                      <Button onClick={() => open("model", m)}>编辑</Button>
                      <Button
                        danger
                        aria-label={`删除模型 ${m.name}`}
                        onClick={() => manageRows("model", "delete", [m])}
                      >
                        删除
                      </Button>
                      <Button
                        loading={!!operations.active[`model-test-${m.id}`]}
                        onClick={() =>
                          execute(
                            async () => {
                              const r = await api(
                                `models/${m.id}/test`,
                                "POST",
                                {},
                              );
                              if (!r.tool_calling)
                                throw new Error("模型未返回有效工具调用");
                              return r;
                            },
                            "模型工具调用验证通过",
                            {
                              key: `model-test-${m.id}`,
                              label: `验证模型「${m.name}」`,
                            },
                          )
                        }
                      >
                        验证工具调用
                      </Button>
                    </Space>
                  ),
                },
              ]}
              scroll={{ x: 800 }}
            />
          </section>
        )}
        {tab === "conversations" && (
          <ConversationPanel
            data={data}
            api={api}
            execute={execute}
            active={operations.active}
          />
        )}
        {tab === "history" && (
          <section className="panel">
            <div className="table-tools">
              <Tabs
                activeKey={historyKind}
                items={[
                  { key: "deliveries", label: "推送记录" },
                  { key: "jobs", label: "处理任务" },
                  { key: "audit", label: "运行轨迹" },
                ]}
                onChange={(v) => {
                  setHistoryKind(v);
                  setHistoryPage(1);
                  setHistoryState("");
                }}
              />
              <Select
                allowClear
                placeholder="全部状态"
                value={historyState || undefined}
                options={Object.entries(STATES).map(([value, [label]]) => ({
                  value,
                  label,
                }))}
                onChange={(v) => {
                  setHistoryState(v || "");
                  setHistoryPage(1);
                }}
                style={{ width: 170 }}
              />
            </div>
            <Table
              rowKey={historyKind === "audit" ? "seq" : "id"}
              dataSource={history.rows}
              pagination={{
                current: historyPage,
                pageSize: 20,
                total: history.total,
                onChange: setHistoryPage,
                showSizeChanger: false,
              }}
              columns={
                historyKind === "audit"
                  ? [
                      { title: "时间", dataIndex: "created", render: when },
                      { title: "对象", dataIndex: "object_id" },
                      { title: "事件", dataIndex: "state" },
                      { title: "详情", dataIndex: "detail", ellipsis: true },
                    ]
                  : [
                      { title: "编号", dataIndex: "id", ellipsis: true },
                      {
                        title: historyKind === "jobs" ? "工作区" : "机器人",
                        render: (_, r) =>
                          historyKind === "jobs"
                            ? workspaceName(r.workspace_id)
                            : botName(r.bot_id),
                      },
                      {
                        title: "状态",
                        dataIndex: "state",
                        render: (value, row) =>
                          historyKind === "jobs" &&
                          value === "QUEUED" &&
                          row.next_try > Date.now() / 1000 ? (
                            <Tag color="orange">等待重试</Tag>
                          ) : (
                            badge(value)
                          ),
                      },
                      { title: "时间", dataIndex: "updated", render: when },
                      {
                        title: "下次尝试",
                        dataIndex: "next_try",
                        render: (value) =>
                          value > Date.now() / 1000 ? when(value) : "—",
                      },
                      { title: "次数", dataIndex: "attempts", width: 68 },
                      { title: "原因", dataIndex: "error", ellipsis: true },
                      {
                        title: "产物",
                        render: (_, r) => (
                          <Button
                            type="link"
                            onClick={() => artifactOpen(r.job_id || r.id)}
                          >
                            查看
                          </Button>
                        ),
                      },
                    ]
              }
              scroll={{ x: 1130 }}
            />
          </section>
        )}
        {tab === "platforms" && (
          <div className="platform-grid">
            {platforms.map((p) => (
              <section className="panel platform-card" key={p.id}>
                <div className="panel-title">
                  <h2>{p.name}</h2>
                  <Tag
                    color={
                      p.implemented
                        ? p.push_capable === false
                          ? "gold"
                          : "green"
                        : "default"
                    }
                  >
                    {p.implemented
                      ? p.push_capable === false
                        ? "仅被动回复"
                        : "已实现接入"
                      : "规划中"}
                  </Tag>
                </div>
                <p>
                  {p.implemented
                    ? {
                        weixin: "扫码登录 · 接收会话上下文",
                        telegram: "Token 配置 · 接收会话发现",
                        webhook: "接收地址 · JSON 推送",
                        discord: "Bot Token · 指定频道发送",
                        slack: "Bot Token · 指定频道或会话发送",
                        onebot: "HTTP API · 群聊或私聊文本发送",
                        qq: "AppID / AppSecret · 官方 WebSocket · 不支持主动订阅推送",
                        feishu: "企业自建应用 · 长连接 · 双向消息",
                        feishu_group: "群 Webhook · 文本推送",
                        wecom_group: "群 Webhook · 文本推送",
                        dingtalk_group: "群 Webhook · 加签及安全关键词",
                      }[p.id]
                    : "适配完成并验证后开放创建。"}
                </p>
                {!!p.inbound_formats?.length && (
                  <div className="platform-formats">
                    {p.inbound_formats.map((format) => (
                      <Tag key={format}>接收{({ text: "文字", image: "图片", voice: "语音", video: "视频" })[format] || format}</Tag>
                    ))}
                    <Tag>回复文字</Tag>
                  </div>
                )}
                {p.implemented && (
                  <Button
                    onClick={() => {
                      open("bot");
                      form.setFieldValue("type", p.id);
                    }}
                  >
                    接入机器人 <ArrowRightOutlined />
                  </Button>
                )}
              </section>
            ))}
          </div>
        )}
      </main>
      <Modal
        open={!!dialog}
        title={
          dialog?.kind === "bot"
            ? "新增机器人"
            : dialog?.kind === "source"
              ? "来源配置"
              : dialog?.kind === "subscription"
                ? "订阅配置"
                : "模型配置"
        }
        width={720}
        destroyOnClose
        onCancel={() => setDialog(null)}
        footer={
          dialog?.kind === "bot" ? (
            <Space>
              {botStep === 1 && (
                <Button onClick={() => setBotStep(0)}>上一步</Button>
              )}
              {botStep === 0 ? (
                <Button
                  type="primary"
                  onClick={async () => {
                    try {
                      await form.validateFields(["name", "type"]);
                      setBotStep(1);
                    } catch {}
                  }}
                >
                  下一步
                </Button>
              ) : botStep === 1 ? (
                <Button
                  key="bot-connect"
                  aria-label="配置并接入"
                  type="primary"
                  loading={busy}
                  onClick={save}
                >
                  配置并接入
                </Button>
              ) : (
                <Button
                  key="bot-finished"
                  aria-label="完成"
                  type="primary"
                  onClick={() => setDialog(null)}
                >
                  完成
                </Button>
              )}
            </Space>
          ) : (
            <Space>
              <Button onClick={() => setDialog(null)}>取消</Button>
              <Button type="primary" loading={busy} onClick={save}>
                保存
              </Button>
            </Space>
          )
        }
      >
        {dialog?.kind === "bot" && (
          <Steps
            size="small"
            current={botStep}
            items={[
              { title: "命名与类型" },
              { title: "平台配置" },
              { title: "自动绑定" },
            ]}
            style={{ margin: "18px 0 28px" }}
          />
        )}
        <Form form={form} layout="vertical" preserve>
          {dialog?.kind === "bot" && (
            <>
              <div style={{ display: botStep === 0 ? "block" : "none" }}>
                <Form.Item
                  name="name"
                  label="机器人名称"
                  rules={[{ required: true, message: "请输入名称" }]}
                >
                  <Input placeholder="例如：研发更新助手" maxLength={80} />
                </Form.Item>
                <Form.Item
                  name="type"
                  label="机器人类型"
                  rules={[{ required: true }]}
                >
                  <Select
                    options={platforms
                      .filter((p) => p.implemented)
                      .map((p) => ({ label: p.name, value: p.id }))}
                  />
                </Form.Item>
              </div>
              {botStep === 1 && (
                <>
                  <div className="connect-heading">
                    <RobotOutlined />
                    <div>
                      <b>{platformName(selectedType)}</b>
                    </div>
                  </div>
                  {selectedType === "qq" && (
                    <Alert
                      type="warning"
                      showIcon
                      style={{ marginBottom: 16 }}
                      message="QQ 官方接口仅用于接收消息与被动回复，不能主动发送订阅快讯。"
                    />
                  )}
                  {selectedPlatform?.fields?.map((f) => (
                    <Form.Item
                      key={f.name}
                      name={["config", f.name]}
                      label={f.label}
                      extra={f.help}
                      rules={
                        f.required
                          ? [{ required: true, message: "请填写" + f.label }]
                          : []
                      }
                    >
                      {f.options ? (
                        <Select options={f.options} />
                      ) : f.secret ? (
                        <Input.Password autoComplete="new-password" />
                      ) : (
                        <Input />
                      )}
                    </Form.Item>
                  ))}
                  <Collapse
                    ghost
                    items={[
                      {
                        key: "workspace",
                        label: "本地工作区设置",
                        children: (
                          <>
                            <Form.Item name="workspace_mode" label="使用方式">
                              <Select
                                options={[
                                  {
                                    value: "independent",
                                    label: "独立目录 · 自动创建",
                                  },
                                  {
                                    value: "shared",
                                    label: "共享目录 · 多机器人共用",
                                  },
                                  ...(workspaces.length
                                    ? [
                                        {
                                          value: "existing",
                                          label: "绑定已有本地工作区",
                                        },
                                      ]
                                    : []),
                                ]}
                              />
                            </Form.Item>
                            {workspaceMode === "shared" &&
                              (joiningShared ? (
                                <Alert
                                  type="info"
                                  showIcon
                                  message={`将加入共享工作区「${joiningShared.name}」`}
                                  style={{ marginBottom: 18 }}
                                />
                              ) : (
                                <Form.Item
                                  name="workspace_name"
                                  label="共享工作区名称"
                                  rules={[
                                    {
                                      required: true,
                                      whitespace: true,
                                      message: "请填写共享工作区名称",
                                    },
                                    { max: 100 },
                                  ]}
                                >
                                  <Input
                                    placeholder="例如：市场资讯、研发更新"
                                    maxLength={100}
                                  />
                                </Form.Item>
                              ))}
                            {workspaceMode === "existing" ? (
                              <Form.Item
                                name="workspace_id"
                                label="已有工作区"
                                rules={[{ required: true }]}
                              >
                                <Select options={workspaceOptions} />
                              </Form.Item>
                            ) : (
                              <Form.Item
                                name="workspace_path"
                                label="本地目录（可选）"
                              >
                                <Input
                                  placeholder={
                                    workspaceMode === "shared"
                                      ? "workspaces/shared/"
                                      : "workspaces/bots/<自动生成 ID>/"
                                  }
                                />
                              </Form.Item>
                            )}
                          </>
                        ),
                      },
                    ]}
                  />
                </>
              )}
              {botStep === 2 && newBot && (
                <div className="onboarding-result">
                  {newBot.status === "WAITING_QR" ? (
                    <>
                      <h3>使用微信扫描二维码</h3>
                      <img
                        className="qr"
                        src={newBot.qr_image}
                        alt="微信登录二维码"
                      />
                      <Spin size="small" /> 等待扫码确认
                    </>
                  ) : newBot.status === "QR_EXPIRED" ? (
                    <>
                      <Alert type="warning" message="二维码已过期" />
                      <Button
                        onClick={async () => {
                          const b = await execute(() =>
                            api(`bots/${newBot.id}/reconnect`, "POST", {}),
                          );
                          if (b) setNewBot(b);
                        }}
                      >
                        刷新二维码
                      </Button>
                    </>
                  ) : newBot.status === "CONNECTION_FAILED" ? (
                    <Alert
                      type="error"
                      message="接入失败"
                      description={newBot.error}
                    />
                  ) : (
                    <>
                      <CheckCircleOutlined className="success-icon" />
                      <h3>机器人已接入，工作区已绑定</h3>
                      {badge(newBot.status)}
                      <h3>{newBot.workspace_name}</h3>
                      <p className="path">{newBot.workspace_path}</p>
                    </>
                  )}
                </div>
              )}
            </>
          )}

          {dialog?.kind === "source" && (
            <>
              <Form.Item
                name="name"
                label="订阅源名称"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item name="type" label="订阅源类型">
                <Select
                  disabled={!!dialog.row}
                  options={[
                    { value: "rss", label: "RSS / Atom" },
                    { value: "webpage", label: "网页变更" },
                    { value: "api", label: "JSON API" },
                    { value: "webhook", label: "Webhook 外部事件" },
                  ]}
                />
              </Form.Item>
              <Form.Item name="category_id" label="分类">
                <Select
                  allowClear
                  placeholder="选择分类"
                  options={categories.map((c) => ({
                    label: c.name,
                    value: c.id,
                  }))}
                  onChange={() => form.setFieldValue("tag_ids", [])}
                />
              </Form.Item>
              <Form.Item
                name="tag_ids"
                label="标签"
              >
                <Select
                  mode="multiple"
                  disabled={!formCategory}
                  options={tagOptions(formCategory)}
                  placeholder="选择该分类下的标签"
                />
              </Form.Item>
              {selectedType !== "webhook" && (
                <>
                  <Form.Item
                    name="url"
                    label="订阅源地址"
                    rules={[
                      { required: true },
                      { type: "url", message: "请输入完整的 HTTP(S) 地址" },
                    ]}
                  >
                    <Input placeholder="https://…" />
                  </Form.Item>
                  <Form.Item
                    name="proxy_mode"
                    label="采集网络"
                    extra="同一网络方式用于订阅列表和正文抓取。使用代理时，请先完成全局采集网络配置。"
                  >
                    <Select
                      placeholder="跟随全局设置"
                      options={[
                        { value: "inherit", label: "跟随全局设置" },
                        { value: "direct", label: "始终直连" },
                        { value: "proxy", label: "使用已配置代理" },
                      ]}
                    />
                  </Form.Item>
                  <Form.Item name="poll_minutes" label="轮询间隔">
                    <Select
                      options={[5, 10, 15, 30, 60].map((value) => ({
                        value,
                        label: value + " 分钟",
                      }))}
                    />
                  </Form.Item>
                  {selectedType === "webpage" && (
                    <Form.Item
                      name="selector"
                      label="监测区域选择器"
                      extra="区域内容变化作为事件；首次检查建立基线，不推送已有内容。"
                    >
                      <Input placeholder="main" />
                    </Form.Item>
                  )}
                  {selectedType === "api" && (
                    <>
                      <Form.Item name="json_path" label="JSON 数据路径">
                        <Input placeholder="例如 data.items，留空读取整个对象" />
                      </Form.Item>
                      <Form.Item name="id_field" label="条目 ID 字段">
                        <Input placeholder="objectID 或 id" />
                      </Form.Item>
                      <Form.Item name="title_field" label="标题字段">
                        <Input placeholder="title" />
                      </Form.Item>
                      <Form.Item name="url_field" label="原文链接字段">
                        <Input placeholder="url" />
                      </Form.Item>
                      <Form.Item name="content_field" label="内容字段">
                        <Input placeholder="story_text 或 content" />
                      </Form.Item>
                      <Form.Item name="published_field" label="发布时间字段">
                        <Input placeholder="created_at" />
                      </Form.Item>
                    </>
                  )}
                  {selectedType === "rss" && (
                    <>
                      <Form.Item
                        name="domains_text"
                        label="文章域名白名单"
                        extra="多个域名使用英文逗号分隔，留空仅允许来源域名。"
                      >
                        <Input />
                      </Form.Item>
                      <Form.Item name="capture_mode" label="内容获取">
                        <Select
                          options={[
                            { value: "full", label: "抓取原文" },
                            { value: "summary", label: "使用订阅摘要" },
                          ]}
                        />
                      </Form.Item>
                      <Form.Item
                        name="allow_undated"
                        label="允许无发布时间的条目"
                        valuePropName="checked"
                        extra="仅对缺少时间戳的来源启用；首次检查只建基线，之后靠稳定条目 ID 去重。"
                      >
                        <Switch />
                      </Form.Item>
                    </>
                  )}
                  <Form.Item
                    name="update_on_change"
                    label="相同条目内容变更再次触发"
                    valuePropName="checked"
                  >
                    <Switch />
                  </Form.Item>
                </>
              )}
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
            </>
          )}
          {dialog?.kind === "subscription" && (
            <>
              <Form.Item
                name="name"
                label="订阅名称"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="workspace_id"
                label="本地工作区"
                rules={[{ required: true }]}
                extra={
                  formWorkspaceId
                    ? receivingBots.length
                      ? `接收机器人：${receivingBots.map((b) => b.name).join("、")}`
                      : "未绑定可主动推送的机器人，当前不会投递"
                    : undefined
                }
              >
                <Select options={workspaceOptions} />
              </Form.Item>
              {dialog.row && !dialog.row.category_id && (
                <Alert
                  type="warning"
                  showIcon
                  message="此订阅仍使用历史指定来源；保存时需重新选择分类和标签。"
                  style={{ marginBottom: 18 }}
                />
              )}
              {!categories.length && (
                <Alert
                  type="info"
                  showIcon
                  message="当前没有已分类的来源，请先在订阅源页归类或从订阅广场导入。"
                  style={{ marginBottom: 18 }}
                />
              )}
              <Form.Item
                name="category_id"
                label="分类"
                rules={[{ required: true, message: "请选择分类" }]}
              >
                <Select
                  placeholder="选择大类"
                  options={categories.map((c) => ({
                    label: c.name,
                    value: c.id,
                  }))}
                  onChange={() => form.setFieldValue("tag_ids", [])}
                />
              </Form.Item>
              <Form.Item
                name="tag_ids"
                label="标签（多选）"
                rules={[
                  {
                    required: true,
                    type: "array",
                    min: 1,
                    message: "请至少选择一个标签",
                  },
                ]}
              >
                <Select
                  mode="multiple"
                  maxTagCount="responsive"
                  disabled={!formCategory}
                  placeholder="选择该分类下的标签"
                  optionFilterProp="label"
                  options={subscriptionTagOptions(formCategory)}
                />
              </Form.Item>
              {!!formTagIds?.length && (
                <Alert
                  type={
                    matchingSources.some((s) => s.enabled) ? "info" : "warning"
                  }
                  showIcon
                  message={`匹配 ${matchingSources.length} 个来源，其中 ${matchingSources.filter((s) => s.enabled).length} 个已启用`}
                  description={matchingSources
                    .slice(0, 5)
                    .map((s) => s.name)
                    .join("、")}
                  style={{ marginBottom: 18 }}
                />
              )}
              <Form.Item name="mode" label="处理方式">
                <Select
                  options={[
                    {
                      value: "agent",
                      label: "Agent 整理 · 按工作区规则和模板",
                    },
                    { value: "forward", label: "直接转发 · 不调用模型" },
                    {
                      value: "rules",
                      label: "规则处理 · 固定格式与长度限制",
                    },
                  ]}
                />
              </Form.Item>
              {processingMode === "agent" && (
                <Form.Item name="model_id" label="模型（可选）">
                  <Select
                    allowClear
                    placeholder="沿用工作区或全局默认"
                    options={modelOptions}
                  />
                </Form.Item>
              )}
              <Form.Item name="enabled" label="启用" valuePropName="checked">
                <Switch />
              </Form.Item>
            </>
          )}
          {dialog?.kind === "model" && (
            <>
              <Form.Item
                name="name"
                label="配置名称"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="base_url"
                label="兼容 API 服务地址"
                rules={[{ required: true }]}
                extra="填写包含 /v1 的基础地址，服务会追加 /chat/completions。"
              >
                <Input placeholder="https://…/v1" />
              </Form.Item>
              <Form.Item
                name="model"
                label="模型标识"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="api_key"
                label={dialog.row ? "新 API Key（留空保留原值）" : "API Key"}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
              <Form.Item name="thinking" label="思考模式（服务支持时生效）">
                <Select
                  allowClear
                  placeholder="模型默认"
                  options={[
                    { value: "default", label: "模型默认" },
                    { value: "disabled", label: "关闭深度思考" },
                    { value: "enabled", label: "开启深度思考" },
                  ]}
                />
              </Form.Item>
              <Form.Item
                name="default"
                label="设为全局默认"
                valuePropName="checked"
              >
                <Switch />
              </Form.Item>
            </>
          )}
        </Form>
      </Modal>
      <TaxonomyPanel
        open={taxonomyOpen}
        onClose={() => setTaxonomyOpen(false)}
        data={data}
        api={api}
        execute={execute}
      />
      <Drawer
        width={650}
        open={!!detail}
        onClose={() => setDetail(null)}
        title="机器人管理"
      >
        {(() => {
          const b = bots.find((x) => x.id === detail);
          if (!b) return null;
          return (
            <>
              <h2>{b.name}</h2>
              <Space>
                {badge(b.enabled ? b.status : "PAUSED")}
                <Tag>{platformName(b.type)}</Tag>
                {b.type === "telegram" && (
                  <Tag>{b.network_mode === "proxy" ? "全局代理" : "直连"}</Tag>
                )}
              </Space>
              {b.error && (
                <Alert
                  type="warning"
                  message={b.error}
                  style={{ margin: "16px 0" }}
                />
              )}
              <div className="detail-block">
                <span>本地工作区</span>
                <p className="path">
                  {b.workspace_path || "连接完成后自动绑定"}
                </p>
                <Select
                  placeholder="绑定已有目录"
                  options={workspaceOptions}
                  value={b.workspace_id || undefined}
                  style={{ width: "100%" }}
                  onChange={(workspace_id) =>
                    execute(
                      () =>
                        api(`bots/${b.id}/update`, "POST", {
                          revision: b.revision,
                          workspace_id,
                        }),
                      "工作区已绑定",
                    )
                  }
                />
              </div>
              <div className="detail-block">
                <span>接收目标</span>
                {b.targets.length ? (
                  <Checkbox.Group
                    value={b.targets.filter((t) => t.selected).map((t) => t.id)}
                    onChange={(target_ids) =>
                      execute(() =>
                        api(`bots/${b.id}/update`, "POST", {
                          revision: b.revision,
                          target_ids,
                        }),
                      )
                    }
                  >
                    {b.targets.map((t) => (
                      <div key={t.id}>
                        <Checkbox value={t.id}>{t.name}</Checkbox>
                      </div>
                    ))}
                  </Checkbox.Group>
                ) : (
                  <p>向机器人发送一条消息，等待接收会话出现。</p>
                )}
              </div>
              {["weixin", "telegram", "qq", "feishu"].includes(b.type) && (
                <div className="detail-block">
                  <span>对话模型</span>
                  <Select
                    aria-label="机器人对话模型"
                    allowClear
                    placeholder="沿用工作区或默认对话模型"
                    value={b.conversation_model_id || undefined}
                    options={models.map((m) => ({
                      value: m.id,
                      label: m.name,
                    }))}
                    style={{ width: "100%" }}
                    onChange={(conversation_model_id) =>
                      execute(
                        () =>
                          api(`bots/${b.id}/update`, "POST", {
                            revision: b.revision,
                            conversation_model_id:
                              conversation_model_id || null,
                          }),
                        "机器人对话模型已更新",
                        {
                          key: `bot-chat-model-${b.id}`,
                          label: "保存机器人对话模型",
                        },
                      )
                    }
                  />
                </div>
              )}
              {b.type !== "weixin" && (
                <Collapse
                  ghost
                  items={[
                    {
                      key: "connection",
                      label: "修改接入配置",
                      children: (
                        <Form
                          layout="vertical"
                          onFinish={(config) =>
                            execute(
                              () =>
                                api(`bots/${b.id}/reconnect`, "POST", {
                                  config,
                                }),
                              "接入配置已验证并更新",
                              {
                                key: `bot-reconnect-${b.id}`,
                                label: `更新机器人「${b.name}」接入`,
                              },
                            )
                          }
                        >
                          {platforms
                            .find((p) => p.id === b.type)
                            ?.fields?.map((f) => (
                              <Form.Item
                                key={f.name}
                                name={f.name}
                                extra={f.help}
                                label={
                                  f.options ? f.label : f.label + "（留空保留）"
                                }
                              >
                                {f.options ? (
                                  <Select options={f.options} />
                                ) : f.secret ? (
                                  <Input.Password autoComplete="new-password" />
                                ) : (
                                  <Input />
                                )}
                              </Form.Item>
                            ))}
                          <Button
                            htmlType="submit"
                            loading={
                              !!operations.active[`bot-reconnect-${b.id}`]
                            }
                          >
                            保存并重新接入
                          </Button>
                        </Form>
                      ),
                    },
                  ]}
                  style={{ marginBottom: 20 }}
                />
              )}
              {b.type === "qq" && (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message="QQ 官方机器人仅能接收消息并被动回复，不会发送订阅快讯或主动测试消息。"
                />
              )}
              <Space wrap>
                <Button
                  icon={<SendOutlined />}
                  disabled={b.type === "qq"}
                  loading={!!operations.active[`bot-test-${b.id}`]}
                  onClick={() =>
                    execute(
                      () => api(`bots/${b.id}/test`, "POST", {}),
                      "测试消息已提交到真实接收目标",
                      {
                        key: `bot-test-${b.id}`,
                        label: `发送「${b.name}」测试消息`,
                      },
                    )
                  }
                >
                  发送测试消息
                </Button>
                <Button
                  loading={!!operations.active[`bot-reconnect-${b.id}`]}
                  onClick={async () => {
                    const v = await execute(
                      () => api(`bots/${b.id}/reconnect`, "POST", {}),
                      undefined,
                      {
                        key: `bot-reconnect-${b.id}`,
                        label: `重新接入「${b.name}」`,
                      },
                    );
                    if (v?.status === "WAITING_QR") {
                      setDetail(null);
                      open("bot");
                      setNewBot(v);
                      setBotStep(2);
                    }
                  }}
                >
                  重新接入
                </Button>
              </Space>
            </>
          );
        })()}
      </Drawer>
      <Drawer
        width={850}
        open={!!artifact}
        onClose={() => setArtifact(null)}
        title="内容与运行证据"
      >
        {artifact && (
          <>
            <Tabs
              activeKey={artifact.kind}
              items={[
                ["report", "推送内容"],
                ["processed", "处理结果"],
                ["raw", "原始输入"],
                ["note", "执行记录"],
                ["trace", "模型与工具"],
                ["event", "事件"],
              ].map(([key, label]) => ({ key, label }))}
              onChange={(kind) => artifactOpen(artifact.job, kind)}
            />
            <p className="path">{artifact.path}</p>
            <pre className="artifact">{artifact.content}</pre>
          </>
        )}
      </Drawer>
    </div>
  );
}
createRoot(document.getElementById("root")).render(
  <ConfigProvider
    locale={zhCN}
    button={{ autoInsertSpace: false }}
    theme={{
      token: {
        colorPrimary: "#255c48",
        colorBgLayout: "#f5f5f0",
        borderRadius: 7,
        fontFamily: 'Inter, "Microsoft YaHei", sans-serif',
        colorText: "#24352e",
        controlHeight: 38,
      },
    }}
  >
    <App>
      <Workbench />
    </App>
  </ConfigProvider>,
);
