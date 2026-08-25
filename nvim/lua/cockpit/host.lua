-- Everything that talks to the conversation registry lives behind here.
local M = {}

local SCRIPT = vim.fn.expand("~/.tmux/scripts/cc-host.sh")

local function run(args)
  local out = vim.system(vim.list_extend({ SCRIPT }, args), { text = true }):wait()
  if out.code ~= 0 then
    vim.notify("cockpit: " .. (out.stderr or "host call failed"), vim.log.levels.ERROR)
    return nil
  end
  return out.stdout
end

local function json(args)
  local raw = run(args)
  if not raw or raw == "" then return {} end
  local ok, decoded = pcall(vim.json.decode, raw)
  return ok and decoded or {}
end

function M.list() return json({ "list" }) end
function M.scan(days) return json({ "scan", tostring(days or 30) }) end

function M.new(topic, label) return vim.trim(run({ "new", topic, label }) or "") end
function M.register(sid, topic, label) return vim.trim(run({ "register", sid, topic, label }) or "") end
function M.start(id) return vim.trim(run({ "start", id }) or "") end
function M.set_topic(id, topic) run({ "topic", id, topic }) end
function M.set_label(id, label) run({ "label", id, label }) end
function M.stop(id) run({ "stop", id }) end      -- park: process ends, entry stays
function M.forget(id) run({ "forget", id }) end  -- delete for good

return M
