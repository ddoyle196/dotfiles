return {
  "nvim-telescope/telescope.nvim",
  opts = {
    pickers = {
      find_files = {
        hidden = true,
        no_ignore = true,
      },
    },
  },
  keys = {
    {
      "<leader>fa",
      function()
        require("telescope.builtin").find_files({ hidden = true, no_ignore = true })
      end,
      desc = "Find All Files (inc. ignored)",
    },
  },
}
