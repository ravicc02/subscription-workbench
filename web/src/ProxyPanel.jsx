import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
} from "antd";
import { SaveOutlined, ApiOutlined } from "@ant-design/icons";

export default function ProxyPanel({ data, api, execute, active }) {
  const [form] = Form.useForm();
  const [dirty, setDirty] = useState(false),
    [revision, setRevision] = useState(null),
    [sourceId, setSourceId] = useState(null);
  const settings = data.network;
  useEffect(() => {
    if (!dirty && (revision === null || settings.revision >= revision)) {
      form.setFieldsValue({ ...settings, password: "", clear_password: false });
      setRevision(settings.revision);
    }
  }, [settings.revision, dirty]);
  const sources = data.sources.filter((s) => s.type !== "webhook" && s.url);
  const chosenId = sources.some((s) => s.id === sourceId)
    ? sourceId
    : sources.find((s) => s.id === "source-fed")?.id || sources[0]?.id;
  const save = async (values) => {
    const result = await execute(
      () => api("network", "POST", { ...values, revision }),
      "采集网络设置已保存，将用于后续订阅列表和正文请求。",
      { key: "proxy-save", label: "保存采集代理" },
    );
    if (result) {
      form.setFieldsValue({ ...result, password: "", clear_password: false });
      setRevision(result.revision);
      setDirty(false);
    }
  };
  const test = async (route) => {
    if (!chosenId) return;
    let config = {};
    if (route === "proxy") {
      try {
        config = await form.validateFields();
      } catch {
        return;
      }
    }
    await execute(
      () => api("network/test", "POST", { route, source_id: chosenId, config }),
      undefined,
      {
        key: "proxy-test",
        label: `测试「${sources.find((s) => s.id === chosenId)?.name}」${route === "proxy" ? "代理" : "直连"}`,
      },
    );
  };
  return (
    <div className="proxy-layout">
      <section className="panel">
        <div className="panel-title">
          <h2>订阅采集代理</h2>
          <Tag color={settings.enabled ? "green" : "default"}>
            {settings.enabled ? "全局默认使用代理" : "全局默认直连"}
          </Tag>
        </div>
        <Alert
          type="info"
          showIcon
          message="仅用于订阅列表与正文抓取。每个订阅源可单独选择跟随全局、直连或使用此代理；模型和机器人通道保持各自网络配置。"
          style={{ marginBottom: 24 }}
        />
        <Form
          form={form}
          layout="vertical"
          onValuesChange={() => setDirty(true)}
          onFinish={save}
        >
          <Form.Item
            name="enabled"
            label="启用全局采集代理"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <div className="profile-grid">
            <Form.Item
              name="scheme"
              label="代理协议"
              rules={[{ required: true }]}
            >
              <Select
                options={[
                  { value: "http", label: "HTTP · 支持 HTTPS 隧道" },
                  { value: "socks5", label: "SOCKS5" },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="port"
              label="代理端口"
              rules={[{ required: true }]}
            >
              <InputNumber min={1} max={65535} style={{ width: "100%" }} />
            </Form.Item>
          </div>
          <Form.Item
            name="host"
            label="代理主机"
            rules={[{ required: true }]}
            extra="填写主机名或 IP，例如 127.0.0.1。不要包含协议、端口或认证信息。"
          >
            <Input placeholder="127.0.0.1" />
          </Form.Item>
          <div className="profile-grid">
            <Form.Item name="username" label="代理用户名（可选）">
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="password"
              label="代理密码（可选）"
              extra={
                settings.has_password
                  ? "已加密保存；留空保留。更换地址或用户名时需重新输入，或清除认证。"
                  : "有认证要求时填写，凭据仅在服务端加密保存。"
              }
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </div>
          {settings.has_password && (
            <Form.Item name="clear_password" valuePropName="checked">
              <Checkbox>清除已保存的代理认证信息</Checkbox>
            </Form.Item>
          )}
          <Form.Item
            name="remote_dns"
            label="通过代理加密解析 DNS"
            valuePropName="checked"
            extra="适用于本机 DNS 解析异常的来源。通过代理访问 Cloudflare DoH，校验解析结果后固定连接到公网 IP；关闭后使用本机 DNS。"
          >
            <Switch />
          </Form.Item>
          <div className="profile-save">
            <span>
              {dirty ? "有未保存的修改" : "当前配置已保存"} · 不会自动切换回直连
            </span>
            <Button
              type="primary"
              aria-label="保存采集网络设置"
              htmlType="submit"
              icon={<SaveOutlined />}
              loading={!!active["proxy-save"]}
            >
              保存设置
            </Button>
          </div>
        </Form>
      </section>
      <section className="panel">
        <h2>
          <ApiOutlined /> 访问测试
        </h2>
        <p>
          使用选定订阅源验证当前表单中的代理，或对比直连。测试不会入队或发送内容。
        </p>
        <Select
          aria-label="代理测试订阅源"
          style={{ width: "100%", marginBottom: 20 }}
          value={chosenId}
          onChange={setSourceId}
          showSearch
          optionFilterProp="label"
          options={sources.map((s) => ({ value: s.id, label: s.name }))}
        />
        <Space wrap>
          <Button
            loading={!!active["proxy-test"]}
            disabled={!chosenId}
            onClick={() => test("proxy")}
          >
            测试此代理
          </Button>
          <Button
            loading={!!active["proxy-test"]}
            disabled={!chosenId}
            onClick={() => test("direct")}
          >
            测试直连
          </Button>
        </Space>
        <div className="proxy-notes">
          <h3>填写示例</h3>
          <p>
            本机代理：主机填 127.0.0.1，端口使用代理软件显示的 HTTP 或 SOCKS5
            监听端口。
          </p>
          <p>
            服务器部署时，127.0.0.1
            指服务器本机，代理服务需要在服务器可访问的位置运行。
          </p>
          <p>
            订阅源指定“使用代理”时会使用这里保存的配置，即使全局默认仍为直连。
          </p>
          <p>
            更换代理不会清除订阅站点的间隔或冷却状态，避免重复请求和触发限流。
          </p>
        </div>
      </section>
    </div>
  );
}
