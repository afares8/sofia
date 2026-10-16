SELECT o.id AS occurrence_id, o.issue_id, o.timestamp AS occurrence_timestamp,
       i.service_id, i.service_name, i.level, i.message, i.source,
       i.environment AS issue_environment, i.release AS issue_release,
       o.detail, o.traceback, o.breadcrumbs
FROM occurrences AS o
JOIN issues AS i ON i.id = o.issue_id
WHERE o.id > :after_id
ORDER BY o.id ASC
LIMIT :batch_size
