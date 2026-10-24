import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  Select,
  Space,
  Switch,
  Table,
  Tag,
} from "antd";
import { ReloadOutlined, SaveOutlined } from "@ant-design/icons";

const STATUS = {
  QUEUED: ["排队中", "gold"],
  RUNNING: ["回答中", "blue"],
  READY: ["待回复", "orange"],
  SENDING: ["发送中", "blue"],
  ADAPTER_ACCEPTED: ["通道已接受", "green"],
  WAITING_CONTEXT: ["等待会话", "orange"],
  PAUSED: ["已暂停", "default"],
  FAILED: ["失败", "red"],
  UNKNOWN: ["结果不确定", "red"],
  CANCELLED: ["已取消", "default"],
};

export default function ConversationPanel({ data, api, execute, active }) {
  const [searchForm] = Form.useForm();
  const [searchRevision, setSearchRevision] = useState(0);
  const [history, setHistory] = useState({ rows: [], total: 0 });
  const [page, setPage] = useState(1);
  const [botId, setBotId] = useState("");
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState("");
  const provider = Form.useWatch("provider", searchForm);
  const loadSearch = () =>
    api("search")
      .then((value) => {
        setSearchRevision(value.revision);
        searchForm.setFieldsValue({ ...value, api_key: "" });
      })
      .catch((e) => setError(e.message));
  const loadHistory = () => {
    if (document.visibilityState === "hidden") return;
    api(
      `history?kind=inbound_messages&page=${page}&bot_id=${encodeURIComponent(botId)}`,
    )
      .then(setHistory)
      .catch((e) => setError(e.message));
  };
  useEffect(() => {
    loadSearch();
  }, []);
  useEffect(() => {
    loadHistory();
    const timer = setInterval(loadHistory, 10000);
    return () => clearInterval(timer);
  }, [page, botId]);
  const models = data.models.map((m) => ({ label: m.name, value: m.id }));
  return (
    <div className="conversation-layout">
      {error && (
        <Alert
          type="error"
          message={error}
          showIcon
          closable
          onClose={() => setError("")}
        />
      )}
      <section className="panel">
        <div className="panel-title">
          <h2>对话模型与实时检索</h2>
        </div>
        <div className="profile-grid">
          <div>
            <label className="form-label">默认对话模型</label>
            <Select
              aria-label="默认对话模型"
              allowClear
              style={{ width: "100%" }}
              placeholder="选择用于回答问题的模型"
              value={data.conversation_model_id || undefined}
              options={models}
              onChange={(conversation_model_id) =>
                execute(
                  () =>
                    api("models/roles", "POST", {
                      conversation_model_id: conversation_model_id || null,
                    }),
                  "默认对话模型已更新",
                  { key: "conversation-model", label: "保存默认对话模型" },
                )
              }
            />
          </div>
        </div>
        <Form
          name="search-settings"
          form={searchForm}
          layout="vertical"
          initialValues={{ provider: "searxng", enabled: false }}
          onFinish={async (values) => {
            const result = await execute(
              () =>
                api("search", "POST", { ...values, revision: searchRevision }),
              "检索配置已保存",
              { key: "search-save", label: "保存检索服务" },
            );
            if (result) {
              setSearchRevision(result.revision);
              searchForm.setFieldValue("api_key", "");
            }
          }}
        >
          <div className="profile-grid">
            <Form.Item name="provider" label="检索服务">
              <Select
                options={[
                  { label: "SearXNG JSON", value: "searxng" },
                  { label: "Brave Search API", value: "brave" },
                ]}
              />
            </Form.Item>
            {provider === "searxng" ? (
              <Form.Item
                name="base_url"
                label="SearXNG 地址"
                extra="本机服务地址须由 WORKBENCH_ALLOWED_HOSTS 显式允许；服务需启用 JSON 格式。"
              >
                <Input placeholder="https://search.example.com" />
              </Form.Item>
            ) : (
              <Form.Item
                name="api_key"
                label="Brave API Key"
                extra="留空保留已保存密钥"
              >
                <Input.Password
                  placeholder="留空保留"
                  autoComplete="new-password"
                />
              </Form.Item>
            )}
          </div>
          <Form.Item
            name="enabled"
            label="启用实时检索"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            aria-label="保存检索配置"
            htmlType="submit"
            loading={!!active["search-save"]}
          >
            保存检索配置
          </Button>
        </Form>
      </section>
      <section className="panel">
        <div className="table-tools">
          <h2>入站与回复</h2>
          <Space>
            <Select
              allowClear
              placeholder="全部机器人"
              value={botId || undefined}
              onChange={(value) => {
                setBotId(value || "");
                setPage(1);
              }}
              options={data.bots.map((b) => ({ label: b.name, value: b.id }))}
              style={{ width: 180 }}
            />
            <Button icon={<ReloadOutlined />} onClick={loadHistory}>
              刷新
            </Button>
          </Space>
        </div>
        <Table
          rowKey="id"
          dataSource={history.rows}
          pagination={{
            current: page,
            pageSize: 20,
            total: history.total,
            onChange: setPage,
            showSizeChanger: false,
          }}
          columns={[
            {
              title: "机器人",
              render: (_, row) =>
                data.bots.find((b) => b.id === row.bot_id)?.name || "已删除",
            },
            { title: "会话目标", dataIndex: "target", ellipsis: true },
            {
              title: "问题",
              render: (_, row) => (
                <Space size={4} wrap>
                  <span>{row.text}</span>
                  {(row.media_types || []).map((kind, index) => (
                    <Tag key={index}>{({ image: "图片", voice: "语音", video: "视频" })[kind] || kind}</Tag>
                  ))}
                </Space>
              ),
            },
            {
              title: "状态",
              dataIndex: "state",
              render: (value) => (
                <Tag color={STATUS[value]?.[1]}>
                  {STATUS[value]?.[0] || value}
                </Tag>
              ),
            },
            {
              title: "时间",
              dataIndex: "updated",
              render: (v) =>
                new Date(v * 1000).toLocaleString("zh-CN", { hour12: false }),
            },
            {
              title: "详情",
              render: (_, row) => (
                <Button type="link" onClick={() => setSelected(row)}>
                  查看
                </Button>
              ),
            },
          ]}
          scroll={{ x: 850 }}
        />
      </section>
      <Drawer
        title="对话记录"
        width={650}
        open={!!selected}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <div className="conversation-detail">
            <p>
              <b>目标：</b>
              {selected.target}
            </p>
            <p>
              <b>提问：</b>
              {selected.text}
            </p>
            {!!selected.media_types?.length && (
              <p><b>媒体：</b>{selected.media_types.map((kind) =>
                ({ image: "图片", voice: "语音", video: "视频" })[kind] || kind
              ).join("、")}</p>
            )}
            <p>
              <b>回答：</b>
              {selected.response || "尚无回答"}
            </p>
            <p>
              <b>状态：</b>
              {STATUS[selected.state]?.[0] || selected.state}
            </p>
            {selected.error && (
              <Alert type="warning" message={selected.error} showIcon />
            )}
            <p>
              <b>模型：</b>
              {data.models.find((m) => m.id === selected.model_id)?.name ||
                "未运行"}
            </p>
            <p>
              <b>工具记录：</b>
            </p>
            <pre className="artifact">
              {JSON.stringify(JSON.parse(selected.tool_trace || "[]"), null, 2)}
            </pre>
          </div>
        )}
      </Drawer>
    </div>
  );
}
