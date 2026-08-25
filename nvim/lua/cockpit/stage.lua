-- One nvim terminal per conversation, each holding a tmux client attached to
-- that conversation's session. Buffers are kept so switching is instant and so
-- a conversation is never re-attached (which would repaint it).
local host = require("cockpit.host")

local M = { bufs = {}, win = nil }

local HINTS = {
  "", "   Pick a conversation on the left.", "",
  "   enter  open it            t  new topic",
  "   tab    show it            n  new conversation",
  "   o      reorder            i  import a past one",
  "   z      fold finished      ?  all keys",
}

function M.set_window(win)
  M.win = win
  local buf = vim.api.nvim_create_buf(false, true)
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, HINTS)
  vim.bo[buf].modifiable = false
  M.placeholder = buf
  M.reset()
end

-- Whenever a conversation leaves the stage the placeholder takes its place, so
-- the window never closes and the split never rearranges itself.
function M.reset()
  if M.win and vim.api.nvim_win_is_valid(M.win)
     and M.placeholder and vim.api.nvim_buf_is_valid(M.placeholder) then
    vim.api.nvim_win_set_buf(M.win, M.placeholder)
  end
end

local function terminal_for(session)
  local buf = M.bufs[session]
  if buf and vim.api.nvim_buf_is_valid(buf) then return buf end

  buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_call(buf, function()
    -- TMUX is unset so tmux does not refuse to nest a client on the same server.
    vim.fn.jobstart({ "sh", "-c", "unset TMUX; exec tmux attach -t " .. vim.fn.shellescape("=" .. session) }, {
      term = true,
      on_exit = function()
        M.bufs[session] = nil
        vim.schedule(function()
          if M.win and vim.api.nvim_win_is_valid(M.win)
             and vim.api.nvim_win_get_buf(M.win) == buf then M.reset() end
        end)
      end,
    })
  end)
  vim.bo[buf].bufhidden = "hide"
  M.bufs[session] = buf
  return buf
end

-- A parked conversation has no process to attach to, so it is resumed first.
local function ensure(row)
  if not row.cold then return true end
  vim.notify("cockpit: resuming " .. row.label, vim.log.levels.INFO)
  return host.start(row.id) ~= ""
end

function M.show(row)
  if not (M.win and vim.api.nvim_win_is_valid(M.win)) then return end
  if not ensure(row) then return end
  vim.api.nvim_win_set_buf(M.win, terminal_for(row.id))
end

function M.focus(row)
  M.show(row)
  if M.win and vim.api.nvim_win_is_valid(M.win) then
    vim.api.nvim_set_current_win(M.win)
    vim.cmd("startinsert")
  end
end

function M.forget(session)
  local buf = M.bufs[session]
  M.bufs[session] = nil
  local on_stage = M.win and vim.api.nvim_win_is_valid(M.win) and vim.api.nvim_win_get_buf(M.win) == buf
  if buf and vim.api.nvim_buf_is_valid(buf) then
    if on_stage then M.reset() end
    pcall(vim.api.nvim_buf_delete, buf, { force = true })
  end
end

return M
