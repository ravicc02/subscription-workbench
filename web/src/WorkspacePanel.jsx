import React, { useEffect, useState, useRef } from "react";
import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
} from "antd";
import {
  FolderOpenOutlined,
  SaveOutlined,
  EditOutlined,
} from "@ant-design/icons";

export default function WorkspacePanel({
  data,
  api,
  execute,
  active,
  onEditSubscription,
  onAddSubscription,
  onDelete,
  onBatch,
}) {
  const [selected, setSelected] = useState(null);
  const [checked, setChecked] = useState([]);
  const [dirty, setDirty] = useState(false);
  const [loadedRevision, setLoadedRevision] = useState(null);
  const [form] = Form.useForm();
  const [renameForm] = Form.useForm();
  const [renameTarget, setRenameTarget] = useState(null);
  const loadedId = useRef(null);
  const { workspaces, bots, subscriptions, models } = data;
  const settings = (w) => ({
    name: w.name,
    model_id: w.model_id || undefined,
    conversation_model_id: w.conversation_model_id || undefined,
    conversation_prompt: w.conversation_profile?.prompt || "",
    web_search_enabled: !!w.conversation_profile?.web_search_enabled,
    ...w.ai_profile,
  });
  useEffect(() => {
    setChecked((ids) =>
      ids.filter((id) => workspaces.some((w) => w.id === id)),
    );
  }, [workspaces.map((w) => w.id).join(",")]);
  const workspace = workspaces.find((w) => w.id === selected) || workspaces[0];
  useEffect(() => {
    if (
      workspace &&
      (loadedId.current !== workspace.id ||
        (!dirty &&
          (loadedRevision === null || workspace.revision >= loadedRevision)))
    ) {
      if (loadedId.current !== workspace.id) setDirty(false);
      loadedId.current = workspace.id;
      form.setFieldsValue(settings(workspace));
      setLoadedRevision(workspace.revision);
    }
  }, [workspace?.id, workspace?.revision, dirty]);
  const switchWorkspace = (id) => {
    if (
      dirty &&
      !window.confirm("当前修改尚未保存，是否放弃修改并切换工作区？")
    )
      return;
    setDirty(false);
    setSelected(id);
  };
  if (!workspace)
    return (
      <section className="panel">
        <Empty description="接入机器人后，本地工作区会自动出现在这里。" />
      </section>
    );
  const attached = bots.filter((b) => b.workspace_id === workspace.id);
  const bindings = subscriptions.filter((s) => s.workspace_id === workspace.id);
  const save = async (values) => {
    const { name, model_id, conversation_model_id, conversation_prompt, web_search_enabled, ...ai_profile } = values;
    const result = await execute(
      () =>
        api(`workspaces/${workspace.id}/settings`, "POST", {
          revision: loadedRevision,
          name,
          model_id: model_id || null,
          conversation_model_id: conversation_model_id || null,
          conversation_profile: { prompt: conversation_prompt || "", web_search_enabled: !!web_search_enabled },
          ai_profile,
        }),
      "工作区配置已保存，新入队任务将使用这套规则。",
      { key: `workspace-save-${workspace.id}`, label: `保存工作区「${name}」` },
    );
    if (result) {
      form.setFieldsValue(settings(result));
      setLoadedRevision(result.revision);
      setDirty(false);
    }
  };
  return (
    <div className="workspace-layout">
      <aside className="workspace-sidebar">
        <div className="workspace-sidebar-title">
          <b>本地工作区</b>
          <Badge count={workspaces.length} color="#527461" />
        </div>
        {!!checked.length && (
          <Button
            danger
            size="small"
            onClick={() =>
              onBatch(
                "workspace",
                "delete",
                workspaces.filter((w) => checked.includes(w.id)),
              )
            }
          >
            删除所选（{checked.length}）
          </Button>
        )}
        <div className="workspace-options">
          {workspaces.map((w) => (
            <div
              className={`workspace-option ${w.id === workspace.id ? "selected" : ""}`}
              key={w.id}
            >
              <Checkbox
                aria-label={`选择工作区 ${w.name}`}
                checked={checked.includes(w.id)}
                onChange={(e) =>
                  setChecked((value) =>
                    e.target.checked
                      ? [...value, w.id]
                      : value.filter((k) => k !== w.id),
                  )
                }
              />
              <button onClick={() => switchWorkspace(w.id)}>
                <FolderOpenOutlined />
                <span>
                  <b>{w.name}</b>
                  <small>
                    {bots.filter((b) => b.workspace_id === w.id).length}{" "}
                    个机器人 ·{" "}
                    {
                      subscriptions.filter((s) => s.workspace_id === w.id)
                        .length
                    }{" "}
                    个订阅
                  </small>
                </span>
              </button>
            </div>
          ))}
        </div>
        <p className="workspace-help">
          选择工作区后，右侧配置只作用于这个目录下的任务。
        </p>
      </aside>
      <section className="panel workspace-content">
        <div className="panel-title">
          <div>
            <h2>{workspace.name}</h2>
            <small className="path">{workspace.path}</small>
          </div>
          <Space>
            <Tag>
              {workspace.sharing_mode === "shared" ||
              attached.length > 1 ||
              workspace.path
                .split(String.fromCharCode(92))
                .join("/")
                .endsWith("/workspaces/shared")
                ? "共享工作区"
                : "独立工作区"}
            </Tag>
            <Button
              aria-label="重命名工作区"
              icon={<EditOutlined />}
              onClick={() => {
                setRenameTarget({
                  id: workspace.id,
                  revision: workspace.revision,
                  name: workspace.name,
                });
                renameForm.setFieldsValue({ name: workspace.name });
              }}
            >
              重命名
            </Button>
            <Button danger onClick={() => onDelete("workspace", workspace)}>
              删除
            </Button>
          </Space>
        </div>
        <Tabs
          items={[
            {
              key: "ai",
              label: "AI 配置",
              children: (
                <>
                  <Alert
                    type="info"
                    showIcon
                    message="Agent 整理使用本工作区的 AI 配置；直接转发和规则处理不调用模型。订阅追加要求只补充当前来源，不改变工作区的关注范围与排除条件。"
                    style={{ marginBottom: 22 }}
                  />
                  {dirty && workspace.revision !== loadedRevision && (
                    <Alert
                      type="warning"
                      message="此工作区已在其他位置更新。当前输入已保留，可重新加载服务器设置。"
                      action={
                        <Button
                          size="small"
                          onClick={() => {
                            form.setFieldsValue(settings(workspace));
                            setLoadedRevision(workspace.revision);
                            setDirty(false);
                          }}
                        >
                          重新加载
                        </Button>
                      }
                      style={{ marginBottom: 16 }}
                    />
                  )}
                  <Form
                    name="workspace-ai"
                    form={form}
                    layout="vertical"
                    onValuesChange={() => setDirty(true)}
                    onFinish={save}
                  >
                    <div className="profile-grid">
                      <Form.Item
                        name="name"
                        label="工作区名称"
                        rules={[{ required: true, max: 100 }]}
                      >
                        <Input maxLength={100} />
                      </Form.Item>
                      <Form.Item name="model_id" label="默认模型">
                        <Select
                          allowClear
                          placeholder="使用全局默认模型"
                          options={models.map((m) => ({
                            value: m.id,
                            label: m.name,
                          }))}
                        />
                      </Form.Item>
                    </div>
                    <Form.Item
                      name="focus_prompt"
                      label="AI 关注范围"
                      extra="描述什么内容值得处理，AI 会结合原文判断相关性；留空表示不过滤主题。"
                    >
                      <Input.TextArea
                        rows={3}
                        maxLength={12000}
                        showCount
                        placeholder="例如：关注国际黄金、白银、原油和主要货币对，以及会影响这些市场的央行政策与地缘事件。"
                      />
                    </Form.Item>
                    <Form.Item
                      name="excluded_topics"
                      label="排除内容"
                      extra="描述应当跳过的内容。排除条件优先于订阅的追加要求。"
                    >
                      <Input.TextArea
                        rows={2}
                        maxLength={8000}
                        showCount
                        placeholder="例如：跳过广告、重复转载、娱乐资讯和只有标题但没有可靠内容的条目。"
                      />
                    </Form.Item>
                    <Form.Item
                      name="processing_prompt"
                      label="整理要求"
                      extra="告诉 AI 如何阅读、提炼和分析。这里配置的要求会被保存到每次任务快照。"
                    >
                      <Input.TextArea
                        rows={5}
                        maxLength={40000}
                        showCount
                        placeholder="例如：先概括已确认事实，再解释可能影响；区分来源观点和自己的推断，不编造实时价格。"
                      />
                    </Form.Item>
                    <Form.Item
                      name="output_template"
                      label="推送内容模板"
                      extra="用自然语言或固定栏目描述收件人应看到的内容，不需要编写 Markdown 文档文件。"
                    >
                      <Input.TextArea
                        rows={6}
                        maxLength={40000}
                        showCount
                        placeholder={
                          "标题：\n摘要：\n原文链接：\n影响与后续关注："
                        }
                      />
                    </Form.Item>
                    <Form.Item name="language" label="输出语言">
                      <Input placeholder="例如：中文" maxLength={80} />
                    </Form.Item>
                    <Form.Item
                      name="additional_instructions"
                      label="补充要求（可选）"
                      extra="原工作区中自定义的其他要求会迁入这里，可直接修改。"
                    >
                      <Input.TextArea rows={3} maxLength={81000} showCount />
                    </Form.Item>
                    <div className="panel-title"><h2>对话 Agent</h2></div>
                    <div className="profile-grid">
                      <Form.Item name="conversation_model_id" label="对话模型">
                        <Select allowClear placeholder="使用默认对话模型" options={models.map((m) => ({ value: m.id, label: m.name }))} />
                      </Form.Item>
                      <Form.Item name="web_search_enabled" label="允许实时检索" valuePropName="checked" extra="仅在全局检索服务已配置时生效。">
                        <Checkbox />
                      </Form.Item>
                    </div>
                    <Form.Item name="conversation_prompt" label="对话风格与边界" extra="仅作用于本工作区的问答，不影响订阅整理。">
                      <Input.TextArea rows={3} maxLength={8000} showCount />
                    </Form.Item>
                    <div className="profile-save">
                      <span>
                        {dirty ? "有未保存的修改" : "当前配置已保存"} ·
                        对新任务生效，已入队任务保留原快照
                      </span>
                      <Button
                        type="primary"
                        htmlType="submit"
                        aria-label="保存工作区设置"
                        icon={<SaveOutlined />}
                        loading={!!active[`workspace-save-${workspace.id}`]}
                      >
                        保存设置
                      </Button>
                    </div>
                  </Form>
                </>
              ),
            },
            {
              key: "subscriptions",
              label: `订阅分配（${bindings.length}）`,
              children: (
                <>
                  <div className="table-tools">
                    <p>选择哪些订阅源进入这个工作区。</p>
                    <Button onClick={() => onAddSubscription(workspace.id)}>
                      添加订阅
                    </Button>
                  </div>
                  <Table
                    rowKey="id"
                    dataSource={bindings}
                    columns={[
                      { title: "订阅", dataIndex: "name" },
                      {
                        title: "订阅源",
                        render: (_, s) => {
                          const names =
                            (
                              s.resolved_source_ids ||
                              s.source_ids ||
                              (s.source_id ? [s.source_id] : [])
                            )
                              .map(
                                (id) =>
                                  data.sources.find((x) => x.id === id)?.name ||
                                  id,
                              )
                              .join("、") ||
                            (s.category_id ? "当前无匹配来源" : "已删除");
                          if (!s.category_id) return names;
                          const category =
                            data.categories?.find((c) => c.id === s.category_id)
                              ?.name || "分类";
                          const tags = (s.tag_ids || []).map(
                            (id) =>
                              data.tags?.find((t) => t.id === id)?.name || id,
                          );
                          return (
                            <div>
                              <b>
                                {category}
                                {tags.length
                                  ? ` · ${tags.join("、")}`
                                  : " · 全部标签"}
                              </b>
                              <small>{names}</small>
                            </div>
                          );
                        },
                      },
                      {
                        title: "处理方式",
                        dataIndex: "mode",
                        render: (x) =>
                          ({
                            agent: "Agent 整理",
                            rules: "规则处理",
                            forward: "直接转发",
                          })[x],
                      },
                      {
                        title: "操作",
                        render: (_, s) => (
                          <Space>
                            <Button onClick={() => onEditSubscription(s)}>
                              编辑
                            </Button>
                            <Button
                              danger
                              onClick={() => onDelete("subscription", s)}
                            >
                              删除
                            </Button>
                          </Space>
                        ),
                      },
                    ]}
                    pagination={false}
                  />
                </>
              ),
            },
            {
              key: "bots",
              label: `绑定机器人（${attached.length}）`,
              children: (
                <div>
                  {attached.length ? (
                    attached.map((b) => (
                      <div className="list-row" key={b.id}>
                        <b>{b.name}</b>
                        <Tag>{b.type}</Tag>
                        <small>{b.enabled ? "已启用" : "已暂停"}</small>
                      </div>
                    ))
                  ) : (
                    <Empty description="此目录当前没有绑定机器人，可以删除工作区登记；本地文件会保留。" />
                  )}
                </div>
              ),
            },
          ]}
        />
      </section>
      <Modal
        open={!!renameTarget}
        title="重命名工作区"
        okText="保存名称"
        cancelText="取消"
        confirmLoading={!!active[`workspace-rename-${renameTarget?.id}`]}
        onCancel={() => setRenameTarget(null)}
        onOk={async () => {
          const values = await renameForm.validateFields();
          const result = await execute(
            () =>
              api(`workspaces/${renameTarget.id}/rename`, "POST", {
                revision: renameTarget.revision,
                name: values.name,
              }),
            "工作区名称已更新，目录、规则和机器人绑定保持不变。",
            {
              key: `workspace-rename-${renameTarget.id}`,
              label: "重命名工作区",
            },
          );
          if (!result) throw new Error("重命名未完成");
          if (workspace.id === result.id) {
            form.setFieldValue("name", result.name);
            setLoadedRevision(result.revision);
          }
          setRenameTarget(null);
        }}
      >
        <Form name="workspace-rename" form={renameForm} layout="vertical">
          <Form.Item
            name="name"
            label="工作区名称"
            rules={[
              { required: true, whitespace: true, message: "请填写工作区名称" },
              { max: 100 },
            ]}
          >
            <Input maxLength={100} autoFocus />
          </Form.Item>
        </Form>
        <p>
          共用此工作区的机器人和订阅会同步显示新名称，本地目录和已有内容保留。
        </p>
      </Modal>
    </div>
  );
}
