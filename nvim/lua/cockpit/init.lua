-- Cockpit: sidebar of your Claude conversations, with the current one docked
-- beside it. See README.md in this folder for how the pieces divide.
local view = require("cockpit.view")
local stage = require("cockpit.stage")
local host = require("cockpit.host")

local M = {}

local defaults = {
  width = 52,
  refresh_ms = 5000,
}

-- One table drives the keymaps, the help window and which-key, so the three
-- can never drift apart.
local KEYS = {
  { group = "Go" },
  { "<CR>",  view.open,        "open it and start typing" },
  { "<Tab>", view.peek,        "show it, stay in the list" },
  { "R",     view.refresh,     "refresh now" },
  { "q",     "close",          "close the cockpit" },
  { "?",     "help",           "this list" },
  { group = "Arrange" },
  { "o",     view.cycle_order, "cycle order: actionable / topic / recent" },
  { "z",     view.toggle_fold, "fold or show finished" },
  { group = "Add" },
  { "t",     view.new_topic,   "new topic" },
  { "n",     view.new_thread,  "new conversation in this topic" },
  { "i",     view.import,      "bring back a past conversation" },
  { group = "Change" },
  { "r",     view.rename,      "rename this conversation" },
  { "T",     view.retopic,     "move it to another topic" },
  { group = "Remove" },
  { "x",     view.park,        "park it (resumable, keeps its place)" },
  { "d",     view.delete_thread, "remove this conversation" },
  { "D",     view.delete_topic,  "remove the whole topic" },
}

-- Colour is never hardcoded: linking to the semantic groups means the cockpit
-- follows whatever colorscheme is loaded, today and after any switch.
local function highlights()
  local link = function(from, to) vim.api.nvim_set_hl(0, from, { link = to, default = true }) end
  link("CockpitAnswer",  "DiagnosticError")
  link("CockpitRunning", "DiagnosticWarn")
  link("CockpitPickup",  "DiagnosticInfo")
  link("CockpitWaiting", "Comment")
  link("CockpitDone",    "NonText")
  link("CockpitRecap",   "Comment")
  link("CockpitTopic",   "Title")
  link("CockpitHeader",  "NonText")
end

local ICON_ORDER = { "answer", "running", "pickup", "waiting", "done", "dead" }

-- The state glyphs are double-width, so byte padding would stagger the column.
local function entry(key, text)
  return ("    %s%s %s"):format(key, string.rep(" ", math.max(1, 8 - vim.fn.strdisplaywidth(key))), text)
end

function M.help()
  local lines = { "", "  Cockpit" }
  for _, k in ipairs(KEYS) do
    if k.group then
      table.insert(lines, "")
      table.insert(lines, "  " .. k.group)
    else
      table.insert(lines, entry(k[1], k[3]))
    end
  end
  table.insert(lines, "")
  table.insert(lines, "  What the marks mean")
  for _, name in ipairs(ICON_ORDER) do
    local s = view.STATE[name]
    table.insert(lines, entry(s.icon, s.name))
  end
  table.insert(lines, entry("·", "parked — opening it resumes where you left off"))
  table.insert(lines, "")

  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  vim.bo[buf].bufhidden = "wipe"
  local width = 62
  local win = vim.api.nvim_open_win(buf, true, {
    relative = "editor", style = "minimal", border = "rounded",
    width = width, height = math.min(#lines, vim.o.lines - 6),
    row = math.floor((vim.o.lines - #lines) / 2),
    col = math.floor((vim.o.columns - width) / 2),
    title = " keys ", title_pos = "center",
  })
  vim.wo[win].cursorline = false
  for _, lhs in ipairs({ "q", "<Esc>", "?" }) do
    vim.keymap.set("n", lhs, "<cmd>close<cr>", { buffer = buf, nowait = true, silent = true })
  end
end

local function keymaps(buf)
  for _, k in ipairs(KEYS) do
    if not k.group then
      local fn = k[2]
      if fn == "close" then fn = function() vim.cmd("tabclose") end end
      if fn == "help" then fn = M.help end
      vim.keymap.set("n", k[1], fn, { buffer = buf, nowait = true, silent = true, desc = "Cockpit: " .. k[3] })
    end
  end
end

function M.open()
  vim.cmd("tabnew")
  local stage_win = vim.api.nvim_get_current_win()
  vim.cmd("topleft vsplit")
  local side_win = vim.api.nvim_get_current_win()
  vim.api.nvim_win_set_width(side_win, M.config.width)

  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_win_set_buf(side_win, buf)
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].filetype = "cockpit"
  vim.bo[buf].modifiable = false
  vim.wo[side_win].number = false
  vim.wo[side_win].relativenumber = false
  vim.wo[side_win].signcolumn = "no"
  vim.wo[side_win].wrap = false
  vim.wo[side_win].cursorline = true
  vim.wo[side_win].winfixwidth = false   -- draggable

  for _, o in ipairs({ "number", "relativenumber", "cursorline" }) do vim.wo[stage_win][o] = false end
  vim.wo[stage_win].signcolumn = "no"
  vim.wo[stage_win].fillchars = "eob: "

  view.buf, view.win = buf, side_win
  stage.set_window(stage_win)
  keymaps(buf)
  view.refresh()

  local grp = vim.api.nvim_create_augroup("Cockpit", { clear = true })
  local timer = vim.uv.new_timer()
  timer:start(M.config.refresh_ms, M.config.refresh_ms, vim.schedule_wrap(function()
    if vim.api.nvim_buf_is_valid(buf) then view.refresh() else timer:stop() end
  end))
  vim.api.nvim_create_autocmd("WinResized", {
    group = grp,
    callback = function() if vim.api.nvim_buf_is_valid(buf) then view.render() end end,
  })
end

-- Reaching the cockpit's actions from anywhere, without it being open first.
local function ensure_open()
  if not (view.win and vim.api.nvim_win_is_valid(view.win)) then M.open() end
end

M.actions = {
  open   = M.open,
  topic  = function() ensure_open(); view.new_topic() end,
  new    = function() ensure_open(); view.new_thread() end,
  import = function() ensure_open(); view.import() end,
  keys   = function() M.help() end,
}

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", defaults, opts or {})
  highlights()
  vim.api.nvim_create_autocmd("ColorScheme", { callback = highlights })
  vim.api.nvim_create_user_command("Cockpit", function(a)
    local action = M.actions[a.args ~= "" and a.args or "open"]
    if not action then return vim.notify("cockpit: no action " .. a.args, vim.log.levels.ERROR) end
    action()
  end, {
    nargs = "?",
    desc = "Claude cockpit",
    complete = function() return vim.tbl_keys(M.actions) end,
  })
end

M.config = defaults
return M
