export const sourceKey = row => row.sourceKey || row.share;

export function scopeRows(rows, scope) {
  return rows.filter(row =>
    (!scope.resourceGroup || row.resourceGroup === scope.resourceGroup)
    && (!scope.account || row.account === scope.account)
    && (!scope.sourceKey || sourceKey(row) === scope.sourceKey));
}

export function changeScope(scope, field, value) {
  if (field === "resourceGroup") return { resourceGroup: value, account: "", sourceKey: "" };
  if (field === "account") return { ...scope, account: value, sourceKey: "" };
  return { ...scope, sourceKey: value };
}
