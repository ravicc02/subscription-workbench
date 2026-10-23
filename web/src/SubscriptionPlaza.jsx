import React, { useEffect, useRef, useState } from "react";
import {
  App,
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Tag,
} from "antd";
import {
  DownloadOutlined,
  ImportOutlined,
  UploadOutlined,
} from "@ant-design/icons";

const types = { rss: "RSS / Atom", webpage: "网页", api: "JSON API" };

export default function SubscriptionPlaza({ data, api, execute, active }) {
  const { modal } = App.useApp();
  const [packages, setPackages] = useState([]);
  const [custom, setCustom] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [exportOpen, setExportOpen] = useState(false);
  const [exportForm] = Form.useForm();
  const fileRef = useRef(null);
  const load = async () => {
    try {
      const result = await api("packages");
      setPackages(result.packages);
      setLoadError("");
    } catch (error) {
      setLoadError(error.message);
    }
  };
  useEffect(() => {
    void load();
  }, []);
  const importPack = (manifest, impact) => {
    const count = manifest.categories.reduce(
      (sum, category) => sum + category.sources.length,
      0,
    );
    modal.confirm({
      title: `同步「${manifest.package.name}」到本地？`,
      content: (
        <div>
          <p>
            包含 {manifest.categories.length} 个分类、{count} 个来源。
          </p>
          <p>
            将新增 {impact.categories_new} 个分类、{impact.tags_new} 个标签、
            {impact.sources_new} 个来源；复用 {impact.sources_existing}{" "}
            个已有来源。
          </p>
          {impact.conflicts.length > 0 && (
            <Alert
              type="error"
              message={`分类冲突：${impact.conflicts.join("、")}`}
            />
          )}
          <p>导入后不会自动创建工作区订阅，也不会推送历史内容。</p>
        </div>
      ),
      okText: "同步到本地",
      okButtonProps: {
        disabled: impact.conflicts.length > 0,
        loading: !!active["package-import"],
      },
      cancelText: "取消",
      onOk: async () => {
        const result = await execute(
          () => api("packages/import", "POST", { manifest }),
          `已同步 ${count} 个来源配置`,
          { key: "package-import", label: "导入订阅包" },
        );
        if (result) {
          setCustom(null);
          await load();
        }
      },
    });
  };
  const chooseFile = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const result = await execute(
      async () => {
        if (file.size > 1_000_000) throw new Error("JSON 文件不能超过 1 MB");
        const manifest = JSON.parse(await file.text());
        const impact = await api("packages/preview", "POST", { manifest });
        return { manifest, impact };
      },
      "订阅包格式已验证",
      { key: "package-file", label: "读取订阅包" },
    );
    if (result) setCustom(result);
  };
  const exportPack = async () => {
    let values;
    try {
      values = await exportForm.validateFields();
    } catch {
      return;
    }
    const result = await execute(
      () => api("packages/export", "POST", values),
      "JSON 订阅包已生成",
      { key: "package-export", label: "导出订阅包" },
    );
    if (!result) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(result, null, 2)], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `${result.package.key}.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
    setExportOpen(false);
  };
  const renderPack = (
    { manifest, preview, source_count },
    customPack = false,
  ) => (
    <article className="plaza-item" key={manifest.package.key}>
      <div className="plaza-item-head">
        <div>
          <span className="eyebrow">
            {customPack ? "本地 JSON" : "内置订阅包"}
          </span>
          <h2>{manifest.package.name}</h2>
          <p>{manifest.package.description}</p>
        </div>
        <Button
          type="primary"
          icon={<ImportOutlined />}
          disabled={preview.conflicts.length > 0}
          onClick={() => importPack(manifest, preview)}
        >
          导入本地
        </Button>
      </div>
      <div className="plaza-metrics">
        {manifest.categories.map((category) => (
          <div key={category.key}>
            <strong>{category.name}</strong>
            <span>
              {category.sources.length} 个来源 · {category.tags.length} 个标签
            </span>
            <div className="plaza-tags">
              {category.tags.map((tag) => (
                <Tag key={tag.key}>{tag.name}</Tag>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="plaza-source-list">
        {manifest.categories.flatMap((category) =>
          category.sources.map((source) => (
            <div key={`${category.key}-${source.url}`}>
              <span>{source.name}</span>
              <Tag>{types[source.type]}</Tag>
            </div>
          )),
        )}
      </div>
      <small>
        共{" "}
        {source_count ??
          manifest.categories.reduce((n, c) => n + c.sources.length, 0)}{" "}
        个来源， 新增 {preview.sources_new} · 复用 {preview.sources_existing}
        {preview.conflicts.length > 0
          ? ` · ${preview.conflicts.length} 个分类冲突`
          : ""}
      </small>
    </article>
  );
  return (
    <>
      <section className="panel plaza-toolbar">
        <div>
          <h2>订阅包</h2>
          <p>先把公开来源同步到本地，再在“订阅”中选择工作区、分类与标签。</p>
        </div>
        <Space wrap>
          <input
            ref={fileRef}
            type="file"
            accept=".json,application/json"
            onChange={chooseFile}
            style={{ display: "none" }}
            aria-label="选择 JSON 订阅包"
          />
          <Button
            icon={<UploadOutlined />}
            loading={!!active["package-file"]}
            onClick={() => fileRef.current?.click()}
          >
            导入 JSON
          </Button>
          <Button
            icon={<DownloadOutlined />}
            onClick={() => setExportOpen(true)}
          >
            导出 JSON
          </Button>
        </Space>
      </section>
      {loadError && (
        <Alert
          type="error"
          showIcon
          message={`读取订阅广场失败：${loadError}`}
          action={
            <Button size="small" onClick={load}>
              重试
            </Button>
          }
          style={{ marginBottom: 18 }}
        />
      )}
      {custom && renderPack(custom, true)}
      {packages.length ? (
        packages.map((item) => renderPack(item))
      ) : (
        <Empty description="暂无内置订阅包" />
      )}
      <Modal
        title="导出订阅包"
        open={exportOpen}
        onCancel={() => setExportOpen(false)}
        onOk={exportPack}
        okText="导出 JSON"
        confirmLoading={!!active["package-export"]}
      >
        <Form form={exportForm} layout="vertical" preserve={false}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input maxLength={80} />
          </Form.Item>
          <Form.Item
            name="key"
            label="稳定标识"
            rules={[
              { required: true },
              {
                pattern: /^[a-z][a-z0-9-]{0,47}$/,
                message: "小写字母开头，仅字母、数字和连字符",
              },
            ]}
          >
            <Input placeholder="例如 my-research" />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea maxLength={500} rows={2} />
          </Form.Item>
          <Form.Item
            name="category_ids"
            label="导出分类"
            rules={[{ required: true, type: "array", min: 1 }]}
          >
            <Select
              mode="multiple"
              options={(data.categories || []).map((c) => ({
                label: c.name,
                value: c.id,
              }))}
              placeholder="选择一个或多个分类"
            />
          </Form.Item>
        </Form>
        <Alert
          type="info"
          showIcon
          message="仅导出公开 RSS、网页和 API 配置及分类标签；不会包含 Bot、模型或 Webhook 凭据。"
        />
      </Modal>
    </>
  );
}
