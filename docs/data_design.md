# AyunOS 第一阶段数据设计草案

以下为第一阶段预计的数据表设计草案，仅用于规划，不创建真实数据库。

## notes

- `id`
- `title`
- `content`
- `source_type`
- `source_id`
- `created_at`
- `updated_at`

用途：存放笔记内容、摘录、整理后的文本和关联来源信息。

## projects

- `id`
- `name`
- `description`
- `status`
- `created_at`
- `updated_at`

用途：存放项目卡片、项目主题和阶段性状态。

## tasks

- `id`
- `project_id`
- `title`
- `description`
- `status`
- `due_date`
- `created_at`
- `updated_at`

用途：存放任务项，并与项目建立关联。

## sources

- `id`
- `source_type`
- `source_name`
- `source_path`
- `imported_at`
- `metadata`

用途：记录导入来源，便于追踪资料来自哪里。

## tags

- `id`
- `name`
- `created_at`

用途：提供统一标签体系，用于笔记、项目和任务的分类检索。
