import React, { useState } from "react";
import {
  App,
  Button,
  Drawer,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Tag,
} from "antd";
import { DeleteOutlined, EditOutlined, PlusOutlined } from "@ant-design/icons";

export default function TaxonomyPanel({ open, onClose, data, api, execute }) {
  const { modal } = App.useApp();
  const [editing, setEditing] = useState(null);
  const [form] = Form.useForm();
  const categories = data?.categories || [];
  const tags = data?.tags || [];
  const sources = data?.sources || [];
  const subscriptions = data?.subscriptions || [];
  const begin = (kind, row = null, categoryId = null) => {
    setEditing({ kind, row });
    form.resetFields();
    form.setFieldsValue(row || { category_id: categoryId });
  };
  const save = async () => {
    let fields;
    try {
      fields = await form.validateFields();
    } catch {
      return;
    }
    const result = await execute(
      () =>
        api(editing.kind === "category" ? "categories" : "tags", "POST", {
          ...editing.row,
          ...fields,
        }),
      "分类标签已保存",
      { key: "taxonomy-save", label: "保存分类标签" },
    );
    if (result) setEditing(null);
  };
  const remove = (kind, row) => {
    const used =
      kind === "category"
        ? sources.filter((s) => s.category_id === row.id).length +
          subscriptions.filter((s) => s.category_id === row.id).length +
          tags.filter((t) => t.category_id === row.id).length
        : sources.filter((s) => (s.tag_ids || []).includes(row.id)).length +
          subscriptions.filter((s) => (s.tag_ids || []).includes(row.id))
            .length;
    modal.confirm({
      title: `删除${kind === "category" ? "分类" : "标签"}「${row.name}」？`,
      content: used
        ? `当前有 ${used} 处引用。请先迁移来源、订阅或下级标签，删除会被阻止。`
        : "仅删除分类配置，不清理历史记录。",
      okText: "删除",
      okButtonProps: { danger: true, disabled: used > 0 },
      cancelText: "取消",
      onOk: () =>
        execute(
          () =>
            api(`taxonomy/${kind}/${row.id}/delete`, "POST", {
              revision: row.revision,
            }),
          "已删除",
          { key: `taxonomy-delete-${row.id}`, label: "删除分类标签" },
        ),
    });
  };
  return (
    <>
      <Drawer title="分类与标签" open={open} onClose={onClose} width={650}>
        <div className="taxonomy-heading">
          <span>来源归属一个分类，可同时属于分类下的多个标签。</span>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => begin("category")}
          >
            新增分类
          </Button>
        </div>
        {categories.map((category) => {
          const members = tags.filter((t) => t.category_id === category.id);
          const feedCount = sources.filter(
            (s) => s.category_id === category.id,
          ).length;
          return (
            <section className="taxonomy-group" key={category.id}>
              <div className="taxonomy-title">
                <div>
                  <strong>{category.name}</strong>
                  <small>
                    {category.key} · {feedCount} 个来源
                  </small>
                </div>
                <Space>
                  <Button
                    icon={<PlusOutlined />}
                    title="新增标签"
                    onClick={() => begin("tag", null, category.id)}
                  >
                    标签
                  </Button>
                  <Button
                    icon={<EditOutlined />}
                    title="编辑分类"
                    onClick={() => begin("category", category)}
                  />
                  <Button
                    danger
                    icon={<DeleteOutlined />}
                    title="删除分类"
                    onClick={() => remove("category", category)}
                  />
                </Space>
              </div>
              {members.length ? (
                <div className="taxonomy-tags">
                  {members.map((tag) => (
                    <span className="taxonomy-tag" key={tag.id}>
                      <Tag>{tag.name}</Tag>
                      <small>
                        {
                          sources.filter((s) =>
                            (s.tag_ids || []).includes(tag.id),
                          ).length
                        }{" "}
                        个来源
                      </small>
                      <Button
                        type="text"
                        size="small"
                        icon={<EditOutlined />}
                        title={`编辑${tag.name}`}
                        onClick={() => begin("tag", tag)}
                      />
                      <Button
                        type="text"
                        danger
                        size="small"
                        icon={<DeleteOutlined />}
                        title={`删除${tag.name}`}
                        onClick={() => remove("tag", tag)}
                      />
                    </span>
                  ))}
                </div>
              ) : (
                <small>暂无标签</small>
              )}
            </section>
          );
        })}
        {!categories.length && (
          <p>暂无分类。创建分类后，即可为来源添加标签。</p>
        )}
      </Drawer>
      <Modal
        title={`${editing?.row ? "编辑" : "新增"}${editing?.kind === "tag" ? "标签" : "分类"}`}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={save}
        okText="保存"
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          {editing?.kind === "tag" && (
            <Form.Item
              name="category_id"
              label="所属分类"
              rules={[{ required: true }]}
            >
              <Select
                disabled={!!editing.row}
                options={categories.map((c) => ({
                  value: c.id,
                  label: c.name,
                }))}
              />
            </Form.Item>
          )}
          <Form.Item name="name" label="显示名称" rules={[{ required: true }]}>
            <Input maxLength={80} />
          </Form.Item>
          {!editing?.row && (
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
              extra="导出包中使用此标识，创建后不可更改。"
            >
              <Input placeholder="例如 finance 或 market-news" />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </>
  );
}
