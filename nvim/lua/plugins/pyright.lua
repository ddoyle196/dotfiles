-- Helper to find python from venv in project root
local function get_python_path(workspace)
  local venv_dirs = { "venv", ".venv", "env", ".env" }
  for _, dir in ipairs(venv_dirs) do
    local path = workspace .. "/" .. dir .. "/bin/python"
    if vim.fn.filereadable(path) == 1 then
      return path
    end
  end
  return nil
end

return {
  {
    "neovim/nvim-lspconfig",
    opts = {
      servers = {
        pyright = {
          before_init = function(_, config)
            local python_path = get_python_path(config.root_dir)
            if python_path then
              config.settings.python.pythonPath = python_path
            end
          end,
        },
      },
    },
  },
}
