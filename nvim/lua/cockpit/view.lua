-- The sidebar buffer: what you scan, and everything you can do from it.
local host = require("cockpit.host")
local stage = require("cockpit.stage")

local M = { buf = nil, win = nil, order = "act", folded = true, rows = {}, sel = nil }

-- Five states, five shapes. Shape carries the meaning as much as colour does,
-- so the column still reads without relying on hue.
local STATE = {
  answer  = { rank = 1, icon = "●", hl = "CockpitAnswer",  name = "answer this" },
  running = { rank = 2, icon = "◐", hl = "CockpitRunning", name = "running" },
  pickup  = { rank = 3, icon = "○", hl = "CockpitPickup",  name = "pick this up" },
  waiting = { rank = 4, icon = "◌", hl = "CockpitWaiting", name = "waiting on someone" },
  done    = { rank = 5, icon = "✓", hl = "CockpitDone",    name = "finished" },
  dead    = { rank = 6, icon = "·", hl = "CockpitDone",    name = "process gone" },
}

local ORDERS = { "act", "topic", "age" }
local ORDER_NAME = { act = "actionability", topic = "topic", age = "recency" }
local NS = vim.api.nvim_create_namespace("cockpit")

M.STATE = STATE

local function st(row) return STATE[row.state] or STATE.pickup end

local function age(row)
  if not row.updated_at or row.updated_at == 0 then return "" end
  local m = math.floor((os.time() - row.updated_at) / 60)
  if m < 60 then return m .. "m" end
  if m < 1440 then return math.floor(m / 60) .. "h" end
  return math.floor(m / 1440) .. "d"
end

local function sorted(rows)
  local r = vim.deepcopy(rows)
  if M.order == "age" then
    table.sort(r, function(a, b) return (a.updated_at or 0) > (b.updated_at or 0) end)
  elseif M.order == "topic" then
    table.sort(r, function(a, b)
      if a.topic ~= b.topic then return a.topic < b.topic end
      return st(a).rank < st(b).rank
    end)
  else
    table.sort(r, function(a, b)
      if st(a).rank ~= st(b).rank then return st(a).rank < st(b).rank end
      return (a.updated_at or 0) > (b.updated_at or 0)
    end)
  end
  -- Finished work always settles at the bottom, whatever the ordering.
  local live, fin = {}, {}
  for _, x in ipairs(r) do
    table.insert((x.state == "done" or x.state == "dead") and fin or live, x)
  end
  return live, fin
end

-- Two lines is the whole budget; a recap cut short says so, so nobody reads a
-- clipped sentence as the finished thought.
local function wrap(text, width)
  local out, line, clipped = {}, "", false
  for word in text:gmatch("%S+") do
    if #line + #word + 1 > width then
      if #out >= 2 then clipped = true; break end
      table.insert(out, line); line = word
    else
      line = (line == "") and word or (line .. " " .. word)
    end
  end
  if #out < 2 and line ~= "" then table.insert(out, line) end
  if clipped and #out > 0 then
    local last = out[#out]
    out[#out] = ((#last + 1 > width) and last:sub(1, width - 1) or last) .. "…"
  end
  return out
end

-- A topic is only as calm as its most demanding conversation.
local function topic_state(rows, topic)
  local best
  for _, r in ipairs(rows) do
    if r.topic == topic and (not best or st(r).rank < st(best).rank) then best = r end
  end
  return best and st(best) or STATE.pickup
end

function M.render()
  if not (M.buf and vim.api.nvim_buf_is_valid(M.buf)) then return end
  local width = (M.win and vim.api.nvim_win_is_valid(M.win))
      and vim.api.nvim_win_get_width(M.win) or 52
  local inner = math.max(20, width - 9)

  local live, fin = sorted(M.rows)
  local lines, marks, index = {}, {}, {}

  local function push(text, hl, row)
    table.insert(lines, text)
    if hl then table.insert(marks, { #lines - 1, hl }) end
    index[#lines] = row
  end

  push(("  order: %s   %s"):format(ORDER_NAME[M.order], M.folded and "finished folded" or "finished shown"),
    "CockpitHeader")
  push("", nil)

  local topic_seen = nil
  local function emit(row, dim)
    if M.order == "topic" and row.topic ~= topic_seen then
      topic_seen = row.topic
      push(("  %s %s"):format(topic_state(M.rows, row.topic).icon, row.topic), "CockpitTopic")
    end
    local s = st(row)
    -- Two glyphs, two questions: is it running, and does it want me.
    local head = ("  %s%s %s"):format(row.cold and "·" or " ", s.icon, row.label)
    local a = age(row)
    if a ~= "" then
      local pad = math.max(1, width - vim.fn.strdisplaywidth(head) - #a - 2)
      head = head .. string.rep(" ", pad) .. a
    end
    push(head, dim and "CockpitDone" or s.hl, row)
    for _, l in ipairs(wrap(row.recap or "", inner)) do
      push("       " .. l, dim and "CockpitDone" or "CockpitRecap", row)
    end
  end

  for _, row in ipairs(live) do emit(row, false) end

  if #fin > 0 then
    push("", nil)
    if M.folded then
      push(("  › %d finished"):format(#fin), "CockpitDone")
    else
      for _, row in ipairs(fin) do emit(row, true) end
    end
  end

  if #live == 0 and #fin == 0 then
    push("  nothing yet — press t to start a topic", "CockpitDone")
    push("  or i to bring back a past conversation", "CockpitDone")
  end

  vim.bo[M.buf].modifiable = true
  vim.api.nvim_buf_set_lines(M.buf, 0, -1, false, lines)
  vim.bo[M.buf].modifiable = false
  vim.api.nvim_buf_clear_namespace(M.buf, NS, 0, -1)
  for _, m in ipairs(marks) do
    vim.api.nvim_buf_set_extmark(M.buf, NS, m[1], 0, { end_row = m[1] + 1, hl_group = m[2] })
  end
  M.index = index
end

function M.refresh()
  M.rows = host.list()
  M.render()
end

function M.current()
  if not M.index then return nil end
  return M.index[vim.api.nvim_win_get_cursor(M.win or 0)[1]]
end

-- actions ------------------------------------------------------------------

function M.open()
  local row = M.current(); if not row then return end
  M.sel = row.id
  stage.focus(row)
  vim.defer_fn(M.refresh, 1500)
end

function M.peek()
  local row = M.current(); if not row then return end
  M.sel = row.id
  stage.show(row)
  vim.defer_fn(M.refresh, 1500)
end

function M.cycle_order()
  local i = 1
  for k, v in ipairs(ORDERS) do if v == M.order then i = k end end
  M.order = ORDERS[(i % #ORDERS) + 1]
  M.render()
end

function M.toggle_fold() M.folded = not M.folded; M.render() end

function M.new_topic()
  vim.ui.input({ prompt = "New topic: " }, function(name)
    if not name or name == "" then return end
    vim.ui.input({ prompt = "First conversation: " }, function(label)
      host.new(name, (label ~= "" and label) or "new conversation")
      vim.defer_fn(M.refresh, 400)
    end)
  end)
end

function M.new_thread()
  local row = M.current()
  if not (row and row.topic) then return M.new_topic() end
  vim.ui.input({ prompt = ("New conversation in %s: "):format(row.topic) }, function(label)
    if label == nil then return end
    host.new(row.topic, (label ~= "" and label) or "new conversation")
    vim.defer_fn(M.refresh, 400)
  end)
end

-- Bring back a Claude conversation that the cockpit has never held: the way
-- back in after a crash, and the way to pull in history worth keeping.
function M.import()
  local found = host.scan(60)
  if #found == 0 then
    return vim.notify("cockpit: no unattached conversations in the last 60 days", vim.log.levels.INFO)
  end
  local items = {}
  for _, r in ipairs(found) do
    local d = math.floor((os.time() - r.updated_at) / 86400)
    table.insert(items, { rec = r, text = ("%3dd  %s"):format(d, r.title) })
  end
  vim.ui.select(items, {
    prompt = "Bring back which conversation?",
    format_item = function(i) return i.text end,
  }, function(choice)
    if not choice then return end
    local topics = {}
    for _, r in ipairs(M.rows) do topics[r.topic] = true end
    vim.ui.input({ prompt = "Topic: ", default = (M.current() or {}).topic or "" }, function(topic)
      if not topic or topic == "" then return end
      host.register(choice.rec.claude_session, topic, choice.rec.title)
      M.refresh()
    end)
  end)
end

function M.rename()
  local row = M.current(); if not row then return end
  vim.ui.input({ prompt = "Label: ", default = row.label }, function(v)
    if not v or v == "" then return end
    host.set_label(row.id, v); M.refresh()
  end)
end

function M.retopic()
  local row = M.current(); if not row then return end
  vim.ui.input({ prompt = "Topic: ", default = row.topic }, function(v)
    if not v or v == "" then return end
    host.set_topic(row.id, v); M.refresh()
  end)
end

-- Parking is the cheap, reversible one: the process goes, the conversation and
-- its place in the list stay, and opening it resumes where it left off.
function M.park()
  local row = M.current(); if not row then return end
  if row.cold then return vim.notify("cockpit: already parked", vim.log.levels.INFO) end
  stage.forget(row.id)
  host.stop(row.id)
  M.refresh()
end

function M.delete_thread()
  local row = M.current(); if not row then return end
  local ok = vim.fn.confirm(
    ("Remove this conversation?\n\n  %s  ·  %s\n\nIt leaves the cockpit for good. Press x instead to park it."):format(row.topic, row.label),
    "&Cancel\n&Remove", 1, "Question")
  if ok ~= 2 then return end
  stage.forget(row.id)
  host.forget(row.id)
  M.refresh()
end

function M.delete_topic()
  local row = M.current(); if not row then return end
  local victims = vim.tbl_filter(function(r) return r.topic == row.topic end, M.rows)
  local names = {}
  for _, v in ipairs(victims) do table.insert(names, "  - " .. v.label) end
  local ok = vim.fn.confirm(
    ("Remove topic %q and all %d conversations?\n\n%s\n\nEvery one listed leaves the cockpit for good.")
      :format(row.topic, #victims, table.concat(names, "\n")),
    "&Cancel\n&Remove", 1, "Question")
  if ok ~= 2 then return end
  for _, v in ipairs(victims) do stage.forget(v.id); host.forget(v.id) end
  M.refresh()
end

return M
